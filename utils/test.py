import os
import shutil
from pathlib import Path

print("当前目录:", os.getcwd())
print("PDF存在:", Path("../asset/华为云OrgID_API接口说明文档_中文版.pdf").exists())
print("MinerU位置:", shutil.which("mineru"))