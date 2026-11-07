"""Bounded software discovery/scoring, independent of models and the Web."""

from pathlib import Path
import threading
import time


# Points represent general coding-workspace coverage, not task success. Each
# capability accepts alternatives; optional ML/GPU packages do not reduce it.
CAPABILITIES = [
    ("基础与安装", "Python", 5, [], ["python3"]),
    ("基础与安装", "Bash", 5, [], ["bash"]),
    ("基础与安装", "包安装", 10, ["pip"], ["uv"]),
    ("数值与数据", "数组计算", 8, ["numpy"], []),
    ("数值与数据", "数据表", 7, ["pandas", "polars"], []),
    ("数值与数据", "科学与符号计算", 5, ["scipy", "sympy"], []),
    ("数值与数据", "数据文件", 5, ["openpyxl", "pyarrow"], []),
    ("绘图与图像", "数据绘图", 10, ["matplotlib", "plotly"], []),
    ("绘图与图像", "图像处理", 8, ["PIL", "cv2"], []),
    ("绘图与图像", "矢量图", 7, ["cairosvg", "svgwrite"], ["dot", "inkscape", "rsvg-convert", "resvg"]),
    ("网络与解析", "第三方 HTTP 库", 5, ["requests", "httpx"], []),
    ("网络与解析", "HTML 解析", 5, ["bs4", "lxml"], []),
    ("网络与解析", "结构化校验", 5, ["pydantic", "jsonschema"], []),
    ("文档", "PDF", 5, ["pypdf", "fitz", "pdfplumber"], []),
    ("文档", "文档生成", 5, ["docx", "pptx", "reportlab"], []),
    ("开发验证", "测试", 5, ["pytest"], []),
]
PACKAGES = {"PIL": "Pillow", "cv2": "opencv-python", "bs4": "beautifulsoup4", "fitz": "PyMuPDF",
            "docx": "python-docx", "pptx": "python-pptx", "sklearn": "scikit-learn"}
OPTIONAL = ["torch", "tensorflow", "jax", "sklearn", "jupyter", "numba"]
# Reviewed for reusable scope using installed distribution metadata. This records
# capabilities, not exact versions, dependency closure, or an import guarantee.
EXTRA_PACKAGES = {
    "ddgs": {"modules":["ddgs"], "commands":[], "purpose":"互联网搜索服务聚合"},
    "networkx": {"modules":["networkx"], "commands":[], "purpose":"图、网络与依赖关系分析"},
    "scikit-image": {"modules":["skimage"], "commands":[], "purpose":"科学图像处理"},
    "imageio": {"modules":["imageio"], "commands":[], "purpose":"图像与视频文件读写"},
    "svgpath2mpl": {"modules":["svgpath2mpl"], "commands":[], "purpose":"SVG 路径与 Matplotlib 图形衔接"},
    "uharfbuzz": {"modules":["uharfbuzz"], "commands":[], "purpose":"复杂文字塑形与排版"},
    "resvg-cli": {"modules":[], "commands":["resvg"], "purpose":"SVG 渲染与 PNG 导出"},
    "curl-cffi": {"modules":["curl_cffi"], "commands":[], "purpose":"libcurl 网络客户端与兼容访问"},
    "onnxruntime": {"modules":["onnxruntime"], "commands":[], "purpose":"ONNX 模型推理，按需使用"},
    "peewee": {"modules":["peewee"], "commands":[], "purpose":"轻量数据库模型与持久任务数据"},
    "psutil": {"modules":["psutil"], "commands":[], "purpose":"进程、CPU、内存与系统资源检查"},
    "tabulate": {"modules":["tabulate"], "commands":[], "purpose":"表格文本输出"},
    "websockets": {"modules":["websockets"], "commands":[], "purpose":"WebSocket 客户端与服务通信"},
    "yfinance": {"modules":["yfinance"], "commands":[], "purpose":"金融行情数据获取，按需使用"},
    "opencv-python-headless": {"modules":["cv2"], "commands":[], "purpose":"无 GUI 环境的 OpenCV 实现"},
}


def collection_packages():
    """Distribution names tracked by baseline/optional coverage, without imports."""
    import re
    modules = {name for _,_,_,names,_ in CAPABILITIES for name in names} | set(OPTIONAL)
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    return {normalize(PACKAGES.get(name,name)) for name in modules} | set(EXTRA_PACKAGES)


INTERVAL = 300


def probe_source():
    modules = {name for _,_,_,names,_ in CAPABILITIES for name in names} | set(OPTIONAL)
    commands = {name for _,_,_,_,names in CAPABILITIES for name in names} | {"git","node","ffmpeg","pdflatex"}
    modules.update(name for item in EXTRA_PACKAGES.values() for name in item["modules"])
    commands.update(name for item in EXTRA_PACKAGES.values() for name in item["commands"])
    return "MODULES = " + repr(sorted(modules)) + "\nBINARIES = " + repr(sorted(commands)) + "\n" + Path(__file__).with_name("inventory_probe.py").read_text()


def evaluate(discovery):
    modules, commands = discovery["modules"], discovery["commands"]
    categories, missing, suggestions = {}, [], []
    for category, name, weight, alternatives, binaries in CAPABILITIES:
        available = [item for item in alternatives if modules.get(item, {}).get("found")]
        available += [item for item in binaries if commands.get(item)]
        group = categories.setdefault(category, {"name": category, "score": 0, "maximum": 0, "capabilities": []})
        group["maximum"] += weight
        group["score"] += weight if available else 0
        group["capabilities"].append({"name": name, "weight": weight, "available": available, "alternatives": alternatives+binaries})
        if not available:
            missing.append(name)
            if alternatives:
                suggestions.append(PACKAGES.get(alternatives[0], alternatives[0]))
    score = sum(item["score"] for item in categories.values())
    return {**discovery, "version": 1, "profile": "general-coding-v1", "status": "ready", "score": score,
            "maximum": 100, "level": "severe" if score < 50 else "partial" if score < 80 else "good",
            "categories": list(categories.values()), "missing_capabilities": missing,
            "host_packages": list(dict.fromkeys(suggestions)), "interval_seconds": INTERVAL,
            "assessment": "模块可发现性与可执行文件覆盖率；未逐包 import 验证，不代表任务成功率。可选 ML 包不计分。"}


class ToolInventoryMonitor:
    """Inspect a live shared sandbox without competing with foreground tools."""
    def __init__(self, sandbox, publish, *, interval=INTERVAL):
        self.sandbox, self.publish, self.interval = sandbox, publish, interval
        self.stopped, self.refresh = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.run, name="tool-inventory", daemon=True)

    def start(self):
        self.thread.start()

    def request(self):
        self.refresh.set()

    def close(self):
        self.stopped.set();self.refresh.set()
        if self.thread.is_alive():
            self.thread.join(6)

    def run(self):
        while not self.stopped.is_set():
            self.refresh.clear()
            started = time.monotonic()
            try:
                result = self.sandbox.session.inspect_tools()
                if result is None:
                    self.stopped.wait(2)  # A foreground tool owns the transport.
                    continue
                if "inventory" not in result:
                    raise ValueError(result.get("error") or "Inventory probe did not finish")
                report = evaluate(result["inventory"])
            except Exception as error:
                report = {"version": 1, "status": "error", "error": str(error)[:500], "interval_seconds": self.interval}
            report.update(checked_at=time.time(), elapsed_seconds=round(time.monotonic()-started, 3))
            if not self.stopped.is_set():
                try:
                    self.publish(report)
                except (OSError, ValueError):
                    pass  # A removed/closed Session must not keep a worker alive.
            deadline = time.monotonic()+self.interval
            while not self.stopped.is_set() and time.monotonic()<deadline:
                if self.refresh.wait(min(1, max(0, deadline-time.monotonic()))):
                    # Bound repeated refreshes, including multiple browser pages.
                    if time.monotonic()-started >= 10:
                        break
                    self.stopped.wait(min(1, max(0, 10-(time.monotonic()-started))))

