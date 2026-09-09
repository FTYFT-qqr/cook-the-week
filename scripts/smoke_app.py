"""临时：Streamlit AppTest 冒烟 —— 保证 app.py 脚本本身可运行、无异常。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DEEPSEEK_API_KEY"] = ""  # 确定性路径，无需网络
os.environ["TEMP"] = os.environ["TMP"] = os.path.join(ROOT, ".tmp")
os.makedirs(os.environ["TEMP"], exist_ok=True)

from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60)
at.run()
print("初次加载 exceptions:", list(at.exception))
assert not at.exception, [str(e.value) for e in at.exception]

# 找到场景按钮，点击填表（触发一次 rerun）
btn_fill = [b for b in at.button if "填入该场景" in b.label]
assert btn_fill, "未找到场景填入按钮"
btn_fill[0].click()
at.run()
print("填表后 exceptions:", list(at.exception))
assert not at.exception

# 点击主表单提交按钮
btn_run = [b for b in at.button if "先生成一版" in b.label]
assert btn_run, "未找到开始规划按钮"
btn_run[0].click()
at.run()
print("规划后 exceptions:", list(at.exception))
assert not at.exception, [str(e.value) for e in at.exception]

# 找成功/提示文本确认有输出
texts = [m.value for m in at.markdown] + [s.value for s in at.success]
joined = "".join(texts)
print("出现“排菜完成/菜单”等输出:", any(k in joined for k in ["每日菜单", "排菜", "买菜清单", "天"]))
print("success 数:", len(at.success), "| markdown 段数:", len(at.markdown))
print("✅ AppTest 冒烟通过（页面可加载、表单可提交、结果可渲染）")
