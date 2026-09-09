# -*- coding: utf-8 -*-
"""端到端测试凭据不得硬编码进仓库，须运行时生成或从环境变量读取。"""

import importlib.util
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_E2E = Path(__file__).resolve().parent / "e2e_stage2_understanding.py"


def _load_e2e():
    spec = importlib.util.spec_from_file_location("e2e_stage2_understanding", _E2E)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


def test_source_has_no_hardcoded_password():
    text = _E2E.read_text(encoding="utf-8")
    check("源码不含 PASSWORD = 字面量", 'PASSWORD = "' not in text
          and "PASSWORD = '" not in text)


def test_generates_and_reuses_credentials(tmp: Path):
    mod = _load_e2e()
    state = tmp / "state.json"
    env = {
        "GTAI_E2E_STAGE2_STATE": str(state),
        "GTAI_E2E_USERNAME": "",
        "GTAI_E2E_PASSWORD": "",
    }
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v:
                os.environ[k] = v
            elif k in os.environ:
                del os.environ[k]
        os.environ["GTAI_E2E_STAGE2_STATE"] = str(state)
        user1, password1 = mod.resolve_credentials()
        check("生成的用户名可用", user1 == "stage2_e2e", user1)
        check("生成的密码足够长", isinstance(password1, str) and len(password1) >= 12)
        check("生成的密码不是用户名本身", password1 != user1)
        user2, password2 = mod.resolve_credentials()
        check("再次读取得到同一组凭据", (user1, password1) == (user2, password2))
        dumped = json.loads(state.read_text(encoding="utf-8"))
        check("凭据写入状态文件", dumped.get("password") == password1)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_env_overrides_generated_password(tmp: Path):
    mod = _load_e2e()
    state = tmp / "state.json"
    override = secrets.token_urlsafe(18)
    os.environ["GTAI_E2E_STAGE2_STATE"] = str(state)
    os.environ["GTAI_E2E_USERNAME"] = "stage2_env_user"
    os.environ["GTAI_E2E_PASSWORD"] = override
    try:
        user, password = mod.resolve_credentials()
        check("环境变量用户名生效", user == "stage2_env_user")
        check("环境变量密码生效", password == override)
        check("环境变量密码不落状态文件",
              not state.is_file() or override
              not in state.read_text(encoding="utf-8"))
    finally:
        os.environ.pop("GTAI_E2E_USERNAME", None)
        os.environ.pop("GTAI_E2E_PASSWORD", None)
        os.environ.pop("GTAI_E2E_STAGE2_STATE", None)


def main() -> int:
    fails = 0
    tmp = Path(tempfile.mkdtemp(prefix="gtai-e2e-creds-"))
    try:
        test_source_has_no_hardcoded_password()
        test_generates_and_reuses_credentials(tmp)
        test_env_overrides_generated_password(tmp)
    except (AssertionError, AttributeError) as exc:
        print(f"  FAIL {exc}")
        fails = 1
    print("结果：端到端凭据检查", "失败" if fails else "通过")
    return fails


if __name__ == "__main__":
    sys.exit(main())
