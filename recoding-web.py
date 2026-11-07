"""Launch the local browser frontend from any working directory."""

try:
    from recoding_web.server import main
except ModuleNotFoundError as error:
    if error.name not in {"recoding_web", "agent", "multiagent", "session"}:
        raise
    raise SystemExit(
        "当前 Python 没有安装项目包，请在项目根目录使用已配置的虚拟环境：\n"
        "  .venv/bin/python3 recoding-web.py --env-file .env -C ./sessions --port 8000\n"
        "若该环境尚未安装项目，先执行：\n"
        "  .venv/bin/python3 -m pip install -e '.[cli]'\n"
        "首次配置详见 docs/first-run.md"
    ) from None

if __name__ == "__main__":
    raise SystemExit(main())
