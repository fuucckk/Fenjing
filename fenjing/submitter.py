"""向某个表格或者路径发出payload的submitter"""

import logging
import subprocess
import html
import re

from contextlib import contextmanager
from pathlib import Path
from typing import List, Callable, Union, NamedTuple, Dict
from urllib.parse import quote

from rich.markup import escape as rich_escape

from .form import Form, fill_form
from .requester import HTTPRequester, TCPRequester
from .const import CALLBACK_SUBMIT

logger = logging.getLogger("submitter")


Tamperer = Callable[[str], str]


def shell_tamperer(shell_cmd: str) -> Tamperer:
    """返回一个新的shell tamperer

    Args:
        shell_cmd (str): 用于修改payload的命令

    Returns:
        Tamperer: 新的Tamperer
    """

    def tamperer(payload: str):
        proc = subprocess.Popen(
            shell_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin and proc.stdout
        proc.stdin.write(payload.encode())
        proc.stdin.close()
        ret = proc.wait()
        if ret != 0:
            raise ValueError(
                f"Shell command return non-zero code {ret} for input {payload}"
            )
        out = proc.stdout.read().decode()
        if out.endswith("\n"):
            logger.info(
                "Tamperer [blue]%s[/] output [blue]%s[/]"
                " ends with '\\n', it may cause some issues.",
                rich_escape(shell_cmd),
                rich_escape(out),
                extra={"markup": True, "highlighter": None},
            )
        return out

    return tamperer


def update_content_length(request: bytes) -> bytes:
    """更新请求体长度

    Args:
        request (bytes): 请求

    Returns:
        bytes: 更新结果
    """
    if request.startswith(b"GET") or request.startswith(b"HEAD"):
        return request
    header, _, body = request.rpartition(b"\r\n\r\n")
    if len(header) == 0:
        return request
    content_length = len(body)
    content_length_header = f"Content-Length: {content_length}".encode()
    result, replace_count = re.subn(
        b"Content-Length:.*", content_length_header, header, 0, re.IGNORECASE
    )
    if replace_count == 0:
        result += b"\r\n" + content_length_header
    return result + b"\r\n\r\n" + body


class HTTPResponse(NamedTuple):
    """解析后的HTTP响应

    Args:
        status_code: 返回值
        text: HTTP的正文
    """

    status_code: int
    text: str


class BaseSubmitter:
    """
    payload提交器，其会发送对应的payload，并获得相应页面的状态码与正文
    其支持增加tamperer, 在发送之前对payload进行编码
    """

    def __init__(self, callback=None):
        self.tamperers: List[Tamperer] = []
        self.callback: Callable[[str, Dict], None] = (
            callback if callback else (lambda x, y: None)
        )

    def add_tamperer(self, tamperer: Tamperer):
        """增加新的tamperer

        Args:
            tamperer (Tamperer): 新的tamperer
        """
        self.tamperers.append(tamperer)

    def submit_raw(self, raw_payload: str) -> Union[HTTPResponse, None]:
        """提交tamperer修改后的payload

        Args:
            raw_payload (str): payload

        Returns:
            Union[HTTPResponse, None]: payload提交结果
        """
        raise NotImplementedError()

    def submit(self, payload: str) -> Union[HTTPResponse, None]:
        """调用tamperer修改payload并提交

        Args:
            raw_payload (str): payload

        Returns:
            Union[HTTPResponse, None]: payload提交结果
        """
        if self.tamperers:
            logger.debug("Applying tampers...")
            for tamperer in self.tamperers:
                payload = tamperer(payload)
        logger.debug("Submit [blue]%s[/]", rich_escape(payload))
        resp = self.submit_raw(payload)
        if resp is None:
            return None
        return HTTPResponse(resp.status_code, html.unescape(resp.text))


class ExtraParamAndDataCustomizable:
    def set_extra_param(self, k: str, v: str):
        """设置需要提交的额外GET参数

        Args:
            k (str): 额外参数的键
            v (str): 额外参数的值
        """
        raise NotImplementedError()

    def unset_extra_param(self, k: str):
        """删除需要提交的额外GET参数

        Args:
            k (str): 额外参数的键
            v (str): 额外参数的值
        """
        raise NotImplementedError()


class TCPSubmitter(BaseSubmitter):
    """根据模板从TCP发送HTTP1.1请求的类"""

    def __init__(
        self,
        requester: TCPRequester,
        pattern: bytes,
        toreplace=b"PAYLOAD",
        urlencode_payload=True,
        tamperers: Union[List[Tamperer], None] = None,
        enable_update_content_length=True,
    ):

        super().__init__()
        self.pattern = pattern
        self.toreplace = toreplace
        self.urlencode_payload = urlencode_payload
        self.req = requester
        self.enable_update_content_length = enable_update_content_length
        if tamperers:
            for tamperer in tamperers:
                self.add_tamperer(tamperer)

    def submit_raw(self, raw_payload):
        if self.urlencode_payload:
            raw_payload = quote(raw_payload)
        request = self.pattern.replace(self.toreplace, raw_payload.encode())
        if self.enable_update_content_length:
            request = update_content_length(request)
        result = self.req.request(request)
        if result is None:
            return None
        code, text = result
        return HTTPResponse(code, text)


class RequestSubmitter(BaseSubmitter):
    """向一个url提交GET或POST数据"""

    def __init__(
        self,
        url: str,
        method: str,
        target_field: str,
        params: Union[Dict[str, str], None],
        data: Union[Dict[str, str], None],
        requester: HTTPRequester,
        tamperers: Union[List[Tamperer], None] = None,
    ):
        """传入目标的URL, method和提交的项

        Args:
            url (str): 目标URL
            method (str): 方法
            target_field (str): 目标项
            params (Union[Dict[str, str], None]): 目标GET参数
            data (Union[Dict[str, str], None]): 目标POST参数
        """
        super().__init__()
        self.url = url
        self.method = method
        self.target_field = target_field
        self.params = params if params else {}
        self.data = data if data else {}
        self.req = requester
        if tamperers:
            for tamperer in tamperers:
                self.add_tamperer(tamperer)

    def submit_raw(self, raw_payload):
        params, data = self.params.copy(), self.data.copy()
        if self.method == "POST":
            data.update({self.target_field: raw_payload})
        else:
            params.update({self.target_field: raw_payload})
        logger.info(
            "Submit [blue]%s method=%s params=%s data=%s[/]",
            rich_escape(self.url),
            self.method,
            rich_escape(repr(params)),
            rich_escape(repr(data)),
            extra={"markup": True, "highlighter": None},
        )

        return self.req.request(
            method=self.method, url=self.url, params=params, data=data
        )


class FormSubmitter(BaseSubmitter, ExtraParamAndDataCustomizable):
    """
    向一个表格的某一项提交payload, 其他项随机填充
    """

    def __init__(
        self,
        url: str,
        form: Form,
        target_field: str,
        requester: HTTPRequester,
        callback: Union[Callable[[str, Dict], None], None] = None,
        tamperers: Union[List[Tamperer], None] = None,
    ):
        """传入目标表格的url，form实例与目标表单项，以及用于提交HTTP请求的requester

        Args:
            url (str): 表格所在的url
            form (Form): 表格的实例
            target_field (str): 目标表单项
            requester (Requester): Requester实例，用于实际发送HTTP请求
        """
        super().__init__(callback)
        self.url = url
        self.form = form
        self.req = requester
        self.target_field = target_field
        self.extra_params = {}
        if tamperers:
            for tamperer in tamperers:
                self.add_tamperer(tamperer)

    def submit_raw(self, raw_payload: str) -> Union[HTTPResponse, None]:
        inputs = {self.target_field: raw_payload}
        resp = self.req.request(
            **fill_form(
                self.url,
                self.form,
                inputs,
                extra_params=self.extra_params,
            )
        )
        self.callback(
            CALLBACK_SUBMIT,
            {
                "type": "form",
                "form": self.form,
                "inputs": inputs,
                "response": resp,
            },
        )
        if resp is None:
            return None
        return HTTPResponse(resp.status_code, resp.text)

    def set_extra_param(self, k: str, v: str):
        self.extra_params[k] = v

    def unset_extra_param(self, k: str):
        del self.extra_params[k]


class PathSubmitter(BaseSubmitter, ExtraParamAndDataCustomizable):
    """将payload进行url编码后拼接在某个url的后面并提交，看见..和/时拒绝提交"""

    def __init__(
        self,
        url: str,
        requester: HTTPRequester,
        callback: Union[Callable[[str, Dict], None], None] = None,
        tamperers: Union[List[Tamperer], None] = None,
    ):
        """传入目标URL和发送请求的Requester

        Args:
            url (str): 目标URL
            requester (Requester): Requester实例
        """
        super().__init__(callback)
        if not url.endswith("/"):
            logger.warning(
                "PathSubmitter get a url that's not ends with '/', appending it.",
                extra={"highlighter": None},
            )
            url += "/"
        self.url = url
        self.req = requester
        self.extra_params = {}
        if tamperers:
            for tamperer in tamperers:
                self.add_tamperer(tamperer)

    def submit_raw(self, raw_payload: str) -> Union[HTTPResponse, None]:
        # python requests would reencode url, resulting in payload being changed
        # that's why we're avoiding spaces and '%'
        if any(w in raw_payload for w in ["/", "..", " ", "%"]):
            logger.info(
                "Don't submit [yellow]%s[/] because it can't be in the path.",
                rich_escape(repr(raw_payload)),
                extra={"markup": True, "highlighter": None},
            )
            return None
        resp = self.req.request(
            method="GET", url=self.url + quote(raw_payload), params=self.extra_params
        )
        self.callback(
            CALLBACK_SUBMIT,
            {
                "type": "path",
                "url": self.url,
                "payload": raw_payload,
                "response": resp,
            },
        )
        if resp is None:
            return None
        return HTTPResponse(resp.status_code, resp.text)

    def set_extra_param(self, k: str, v: str):
        self.extra_params[k] = v

    def unset_extra_param(self, k: str):
        del self.extra_params[k]


class JsonSubmitter(BaseSubmitter, ExtraParamAndDataCustomizable):
    """将payload放在如{"name": "xiaoming", "age": 18}这样的JSON中提交"""

    def __init__(
        self,
        url: str,
        method: str,
        json_obj: dict,
        key: str,
        requester: HTTPRequester,
        callback: Union[Callable[[str, Dict], None], None] = None,
        tamperers: Union[List[Tamperer], None] = None,
    ):
        super().__init__(callback)
        self.url = url
        self.method = method
        self.json_obj = json_obj
        self.key = key
        self.req = requester
        self.extra_params = {}
        if tamperers:
            for tamperer in tamperers:
                self.add_tamperer(tamperer)

    def submit_raw(self, raw_payload: str) -> Union[HTTPResponse, None]:
        json_data = {**self.json_obj, self.key: raw_payload}
        resp = self.req.request(
            method=self.method, url=self.url, params=self.extra_params, json=json_data
        )
        self.callback(
            CALLBACK_SUBMIT,
            {
                "type": "json",
                "url": self.url,
                "json": json_data,
                "response": resp,
            },
        )
        if resp is None:
            return None
        return HTTPResponse(resp.status_code, resp.text)

    def set_extra_param(self, k: str, v: str):
        self.extra_params[k] = v

    def unset_extra_param(self, k: str):
        del self.extra_params[k]


# TODO: remove me
class IOSubmitter(BaseSubmitter):
    """将payload保存在本地文件中"""

    def __init__(self, path: Union[Path, None]):
        super().__init__(callback=None)
        self.path = path
        self.is_do_saving = True

    @contextmanager
    def stop_saving(self):
        self.is_do_saving = False
        yield
        self.is_do_saving = True

    def submit_raw(self, raw_payload: str) -> Union[HTTPResponse, None]:
        """假提交函数，作用是将payload保存在文件中，返回的HTTPResponse是假的

        Args:
            raw_payload (str): _description_

        Returns:
            Union[HTTPResponse, None]: _description_
        """
        if self.is_do_saving and isinstance(self.path, Path):
            try:
                self.path.write_text(raw_payload)
            except IOError as e:
                logger.error(
                    "[red bold]Failed to save file[/] [blue]%s[/]",
                    rich_escape(self.path.as_posix()),
                    extra={"markup": True, "highlighter": None},
                )
                raise e
            logger.info(
                "Saved to file [blue]%s[/]",
                rich_escape(self.path.as_posix()),
                extra={"markup": True, "highlighter": None},
            )
        elif self.is_do_saving:
            print(raw_payload)
        return HTTPResponse(250, raw_payload)


Submitter = BaseSubmitter
