# llmirror 构建说明

运行项目：Windows 双击根目录 start.bat；Linux 使用 bash start.sh。

重新构建 Windows 程序，需要 Windows x64 和 Python 3.12：

```text
python -m venv .buildenv
.buildenv\Scripts\python.exe -m pip install -r tools\requirements-build.txt
.buildenv\Scripts\python.exe tools\build_windows.py
```

生成文件为 dist/llmirror.exe。停止运行实例后，用它替换项目根目录的 llmirror.exe，保留 data 和 server.json。程序优先使用根目录 static 文件，缺失时使用打包的静态资源。

Linux 不需要构建 exe，使用运行源码即可。构建目录、虚拟环境、缓存和个人数据无需分发。
