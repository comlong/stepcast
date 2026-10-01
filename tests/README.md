# 回归测试

```bat
.venv\Scripts\python.exe -X utf8 tests\run_all.py           :: 常规 23 组，约 20 分钟
.venv\Scripts\python.exe -X utf8 tests\run_all.py -k reveal :: 只跑名字里带 reveal 的
.venv\Scripts\python.exe -X utf8 tests\run_all.py --lint    :: 先跑 ruff（.venv\Scripts\pip install ruff）
.venv\Scripts\python.exe -X utf8 tests\run_all.py --exe     :: 再测 dist\StepCast（先 build_exe.bat 打包）
.venv\Scripts\python.exe -X utf8 tests\run_all.py --ppt     :: 再测真 PowerPoint（PowerPoint 开着时自动跳过）
```

每组测试结束后打印「通过 / 失败」，完整输出在 `tests/_work/run/<测试名>.log`。

## 规矩

- **不碰用户的东西**：每个测试在 `tests/_work/run/` 下用自己的数据目录（`VT_DATA_DIR`）和自己的
  `config.json`（改 `config.CONFIG_PATH`），不读写用户的 `projects/` 和 `config.json`。
- **不花钱**：大模型用假客户端，云端配音用假 key 和假接口，不发真实请求。
- **不动 PowerPoint**：除了 `test_reveal_ppt`，其他测试都设 `VT_DISABLE_POWERPOINT=1`；
  `test_reveal_ppt` 只在 PowerPoint 没开着时跑，用完只关它自己开的。
- 需要一个完整的「网页录制」项目时用 `fixtures_build.make_capture_project(data_dir)`：
  PIL 画的假后台页面 + ffmpeg 生成的音调，8 步，约 70 秒，不用任何真实项目。

## 目录

| 路径 | 内容 | 提交 |
|---|---|---|
| `test_*.py` | 各组测试。用法都是 `python -X utf8 test_x.py <工作目录>` | 是 |
| `run_all.py` | 一次跑全部 | 是 |
| `fixtures/` | 通用素材：短音频、测试图案视频、几份简单 PPT/PDF。`vid/make_video_decks.py` 能重新生成两份带视频的 PPT | 是 |
| `fixtures_build.py` | 造假的网页录制项目 | 是 |
| `stab_server.py` | `test_stability` 起的真实服务进程 | 是 |
| `ui_server.py`、`ui_seed*.py` | 手工看界面用的测试服务（8770 端口，数据在 `_work/ui_data`），`.claude/launch.json` 里的 `ui-test` | 是 |
| `private/` | 内部资料（公司真实 PPT 和依赖它的测试段落），`test_reveal_ppt` 有就一起测 | **否** |
| `_work/` | 运行时的工作目录 | 否 |

## 加新测试

照现有文件的开头写：先设 `VT_DATA_DIR`、把 `ROOT` 放进 `sys.path`、改 `config.CONFIG_PATH`，
再 import 后端；每条检查用 `check(名字, 条件, 细节)`，最后打印「全部通过」或「失败 N 项：…」。
`run_all.py` 按输出里的 `[FAIL]`、`Traceback`、`失败 N 项` 判断失败。写好后把名字加进 `run_all.py` 的 `TESTS`。
