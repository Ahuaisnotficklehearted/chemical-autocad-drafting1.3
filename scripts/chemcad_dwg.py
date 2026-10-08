#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""chemcad_dwg.py —— 把 DXF 转成 **DWG**（出图格式按 DWG 交付）。

**为什么要单独一层，而不是改写 DXF 写出器**

DWG 是 Autodesk 的**专有二进制格式**，纯 Python 写不出来（ezdxf 也只能读、不能写）。
所以路径只能是"先写 DXF（纯 Python、零依赖、可控），再交给 Autodesk 的转换器
落到 DWG"。这一层就干这件事。

**2026-09-23 变更：出图通道强制走 COM（连到 AutoCAD）**

用户规定：本技能必须用 **COM 端口连到 AutoCAD** 出图。`accoreconsole` 从"默认通道"
退为**显式逃生口**（要 `CHEMCAD_DWG_BACKEND=accore` 才会走）。

为什么这条要写成硬规定而不是"能用就用"：静默换通道是**最难查的一类失真**——
两张图都能画出来、都不报错，但 COM 通道画进的是用户眼前那张图纸（图层/图块/字体
与他的样板一致），accoreconsole 通道落的是无界面核心环境的产物（用默认样板，
线型 `HIDDEN`、字体、图层标准常常对不上）。用户按 COM 的心智模型去用图，
实际拿到的却是另一条通道的产物，这种错在下游才爆。所以要**要么明说走哪条，要么报错**。

**两条硬纪律**

1. **不许静默降级**：要求 DWG 就必须得到 DWG。一个通道都连不上时**报错退出**，
   而不是默默给一个 DXF——"交付格式不对但没人知道"比"出不来图"更糟
   （下游拿 DWG 的人会直接崩，而且是在他那边崩，你这边一切正常）。
2. **不许静默换通道**：默认只走 COM。COM 连不上就说"连不上、怎么修"，
   不悄悄换 accoreconsole 交差。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

#: AutoCAD **正忙/正显示模态对话框**时 COM 拒收调用的重试策略（次数 / 间隔秒）
COM_RETRY = 3
COM_RETRY_WAIT = 1.5

#: AutoCAD 常见安装根（accoreconsole.exe 在其中某版本的子目录里）
_ACAD_ROOTS = (
    r"C:\Program Files\Autodesk",
    r"C:\Program Files (x86)\Autodesk",
    r"D:\CAD",
    r"D:\Program Files\Autodesk",
    r"C:\CAD",
)


def find_accoreconsole() -> Optional[str]:
    """找 `accoreconsole.exe`。先在常见安装根下按版本倒序找，再退回 PATH。"""
    cands: List[str] = []
    for root in _ACAD_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            subs = sorted(os.listdir(root), reverse=True)
        except OSError:
            continue  # 该候选目录不可列 → 试下一个安装路径
        for d in subs:
            for p in (os.path.join(root, d, "accoreconsole.exe"),
                      os.path.join(root, d, "acadcoreconsole.exe")):
                if os.path.isfile(p):
                    cands.append(p)
    p = shutil.which("accoreconsole.exe") or shutil.which("accoreconsole")
    if p:
        cands.append(p)
    return cands[0] if cands else None


def _acad_version_token() -> str:
    """另存 DWG 时的格式代号。用 2018 作默认（兼容面最广的现代版本）。"""
    return os.environ.get("CHEMCAD_DWG_VERSION", "2018")


def dxf_to_dwg_accore(dxf: str, dwg: str, *, timeout: int = 300,
                      accore: Optional[str] = None) -> Dict[str, object]:
    """用 accoreconsole 把 DXF 另存为 DWG。返回报告 dict（不抛异常）。"""
    exe = accore or find_accoreconsole()
    if not exe:
        return {"ok": False, "backend": "accoreconsole",
                "reason": "找不到 accoreconsole.exe（未安装 AutoCAD？）"}
    # ⚠️ 中文路径在控制台里容易翻车：统一先用 ASCII 临时目录过一手，最后再搬回去。
    tmpd = tempfile.mkdtemp(prefix="chemcad_dwg_")
    src = os.path.join(tmpd, "in.dxf")
    dst = os.path.join(tmpd, "out.dwg")
    shutil.copyfile(dxf, src)
    scr = os.path.join(tmpd, "save.scr")
    with open(scr, "w", encoding="ascii", errors="replace") as f:
        f.write(f"FILEDIA\n0\n_SAVEAS\n{_acad_version_token()}\n{dst}\nQUIT\n")
    try:
        p = subprocess.run([exe, "/i", src, "/s", scr],
                           capture_output=True, text=True, errors="replace",
                           timeout=timeout)
        if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
            shutil.copyfile(dst, dwg)
            return {"ok": True, "backend": "accoreconsole", "exe": exe,
                    "bytes": os.path.getsize(dwg), "rc": p.returncode}
        tail = ((p.stdout or "") + (p.stderr or ""))[-400:]
        return {"ok": False, "backend": "accoreconsole", "exe": exe,
                "rc": p.returncode, "reason": f"accoreconsole 未产出 DWG；{tail.strip()}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "backend": "accoreconsole", "exe": exe,
                "reason": f"accoreconsole 超时（{timeout}s）"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "backend": "accoreconsole", "exe": exe,
                "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def dwg_to_dxf(dwg: str, dxf: Optional[str] = None, *, timeout: int = 300
               ) -> Dict[str, object]:
    r"""**DWG → DXF**（读图方向，与 `to_dwg` 反向）。

    纯 Python 读不了 DWG（ezdxf 只能读、也要 ODA 插件才认二进制 DWG），
    所以"剖析 / 校核 / 目视"一条链要能吃 DWG，就得先落一个 DXF 中间文件。

    ⚠️ 走的是 **accoreconsole**，不是 COM：这是**读**方向（打开别人的图另存），
    COM 那条硬规定管的是**出图**方向（把本技能画的图交到用户眼前那份图纸里）。
    读方向不涉及样板一致性，用无界面转换反而更稳。

    实测可用的脚本序列（`FILEDIA 0` + `DXFOUT <路径>` + 一个空行收默认版本）：
    写成 `-DXFOUT` 会被当成"命令行版命令"而要求选项、直接中止；
    写成 lisp 走 `/l` 在 accoreconsole 里也不执行。**只有这个序列实测能出文件。**
    """
    dxf = dxf or os.path.splitext(dwg)[0] + ".dxf"
    exe = find_accoreconsole()
    if not exe:
        return {"ok": False, "backend": "accoreconsole",
                "reason": "找不到 accoreconsole.exe（未安装 AutoCAD？）"}
    # ⚠️ 中文路径在控制台里容易翻车：统一先用 ASCII 临时目录过一手，最后再搬回去。
    tmpd = tempfile.mkdtemp(prefix="chemcad_dxf_")
    try:
        src = os.path.join(tmpd, "in.dwg")
        dst = os.path.join(tmpd, "out.dxf")
        shutil.copyfile(dwg, src)
        scr = os.path.join(tmpd, "x.scr")
        with open(scr, "w", encoding="ascii", errors="replace") as f:
            f.write(f"FILEDIA\n0\nDXFOUT\n{dst}\n\nQUIT\n")
        p = subprocess.run([exe, "/i", src, "/s", scr],
                           capture_output=True, text=True, errors="ignore",
                           timeout=timeout)
        if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
            shutil.copyfile(dst, dxf)
            return {"ok": True, "backend": "accoreconsole", "exe": exe,
                    "path": dxf, "bytes": os.path.getsize(dxf), "rc": p.returncode}
        return {"ok": False, "backend": "accoreconsole", "exe": exe,
                "rc": p.returncode,
                "reason": "accoreconsole 未产出 DXF（脚本序列不匹配？）"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "backend": "accoreconsole",
                "reason": f"accoreconsole 超时（{timeout}s）"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "backend": "accoreconsole",
                "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


@contextmanager
def reading(p: str):
    """上下文管理器版的 `readable_path`：`.dwg` 转临时 DXF，退出即清理。

    ```python
    with DWG.reading("图.dwg") as src:      # src 一定是可读的 DXF 路径
        r = parse(src)
    ```
    """
    src, tmpd = readable_path(p)
    try:
        yield src
    finally:
        if tmpd:
            shutil.rmtree(tmpd, ignore_errors=True)


def readable_path(p: str, *, keep: bool = False) -> Tuple[str, Optional[str]]:
    """给"读图"一侧用：`.dwg` 先转成临时 DXF，`.dxf` 原样返回。

    返回 `(可读路径, 临时目录 or None)`——临时目录交给调用方 `shutil.rmtree`，
    因为剖析/校核/目视往往要在同一次调用里反复读。
    """
    if not str(p).lower().endswith(".dwg"):
        return p, None
    tmpd = tempfile.mkdtemp(prefix="chemcad_read_")
    dxf = os.path.join(tmpd, "read.dxf")
    rep = dwg_to_dxf(p, dxf)
    if not rep.get("ok"):
        shutil.rmtree(tmpd, ignore_errors=True)
        raise RuntimeError(f"读不了 DWG（{os.path.basename(p)}）：{rep.get('reason')}")
    return dxf, tmpd


#: AutoCAD COM 的 ProgID（各版本共用这一个；`AutoCAD.Application.N` 才是版本锁定）
ACAD_PROGID = "AutoCAD.Application"


def com_session(create_if_not_exists: bool = False, *, visible: bool = True):
    """取一个 AutoCAD COM 会话。**连不上就抛 `RuntimeError`**，不带兜底。

    `create_if_not_exists=False` 时只认**已经在跑**的 AutoCAD（`GetActiveObject`）——
    这是默认：出图要画进用户眼前那张图纸，而不是偷偷另起一个实例。
    """
    try:
        import win32com.client as win32       # type: ignore
    except Exception as exc:                                  # noqa: BLE001
        raise RuntimeError(
            f"未安装 pywin32（{type(exc).__name__}）——COM 通道不可用。\n"
            "  解决：`pip install pywin32`，装完跑一次 "
            "`python -c \"import win32com.client\"` 确认。") from exc
    # ★ 接入方法（借自 MCP 连接 Aspen 的实战经验）：**先显式初始化本线程的 COM 单元**。
    # 不初始化时，从非主线程 / 被 MCP 之类的父进程拉起的子进程里调 COM 会失败，
    # 且失败表象与"AutoCAD 没开"极像，容易误判成环境问题而白查半天。
    # 重复初始化在同一线程是安全的（计数式）；本技能是短任务，不再配对 CoUninitialize。
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:                                         # noqa: BLE001
        pass  # 已初始化 / 非 Windows → 无需处理
    try:
        app = win32.GetActiveObject(ACAD_PROGID)
        return app, "running"
    except Exception:                                         # noqa: BLE001
        if not create_if_not_exists:
            raise RuntimeError(
                "COM 连不上 AutoCAD：当前没有正在运行的 AutoCAD 实例。\n"
                "  解决（任选一）：① 手动打开 AutoCAD 再重跑；"
                "② 加 `--create` 让本技能替你启动一个；"
                "③ 确需无界面转换时设环境变量 CHEMCAD_DWG_BACKEND=accore（逃生口，"
                "产物用的是默认样板，字体/线型可能与你眼前那份不一致）。")
    try:
        app = win32.Dispatch(ACAD_PROGID)
    except Exception as exc:                                  # noqa: BLE001
        raise RuntimeError(
            f"COM 无法创建 AutoCAD 实例：{type(exc).__name__}: {exc}\n"
            "  解决：确认本机装了 AutoCAD（不是 AutoCAD LT / DWG TrueView），"
            "且不是以管理员权限隔离启动。") from exc
    try:
        app.Visible = visible
    except Exception:                                         # noqa: BLE001
        pass
    return app, "created"


def _is_com_rejected(exc: BaseException) -> bool:
    """判断一次 COM 失败是不是"**AutoCAD 正忙**"（可重试的环境态），而不是代码错。

    实测 AutoCAD 忙碌 / 有模态对话框时会拒收一切 COM 调用，表现为两种：
      ① `com_error(-2147418111, '被呼叫方拒绝接收呼叫。')`（RPC_E_CALL_REJECTED）；
      ② `AttributeError: <unknown>.Open / .Count / .Add`
         ——代理对象拿到了，但方法调用被拒（`Documents` 集合整体不可用）。
    这两种都**等 AutoCAD 空下来就能成**，不该让强制通道一次被拒就判死。
    """
    s = f"{type(exc).__name__}: {exc}"
    return ("-2147418111" in s or "被呼叫方拒绝" in s
            or ("AttributeError" in s and "<unknown>." in s))


def _wake_autocad(app) -> bool:
    r"""COM 被拒时的**自救**：让 AutoCAD 离开「开始」选项卡，切到一张真图纸上。

    真因（2026-09-24 实测，AutoCAD 2026 / `25.1s`）：活动页是「开始」选项卡时
    `GetAcadState().IsQuiescent` **恒为 False**，于是所有 COM 调用被拒——表现为
    `AttributeError: <unknown>.Open / .Count` 与 `com_error(-2147418111, 被呼叫方拒绝)`。
    这**不是"忙一会儿"**（等再久也不会自己好），也**不是模态对话框**（窗口表里没有
    `#32770`），只有让一张真图纸成为活动页才会重新静默；人工做法就是在 AutoCAD
    里点一下图纸页标。这里用纯 Win32 做同一件事：

      ① `app.HWND`（这个属性即使别的调用被拒也能取到）→ 找它的 `MDIClient`；
      ② 枚举 MDI 子窗，**跳过**标题为「开始 / Start / 起步」的选项卡，挑第一张真图纸；
      ③ `SendMessageTimeout(MDIClient, WM_MDIACTIVATE, hwnd, 0)` 把它激活。

    **只切焦点，不关图纸、不点按钮、不敲键**（用户手工数据的安全边界），
    失败一律吞掉返回 False——自救不成还有原来的报错文案兜底。
    """
    WM_MDIACTIVATE = 0x0222
    SKIP_TITLES = ("开始", "start", "起步", "home", "getting started")
    try:
        import win32gui                                       # pywin32（与 COM 同源）
        import win32con
        root = int(app.HWND)
        if not root:
            return False
        mdi = None
        kids: List[int] = []
        win32gui.EnumChildWindows(
            root, lambda h, _: (kids.append(h), True)[1], None)
        for h in kids:
            if win32gui.GetClassName(h) == "MDIClient":
                mdi = h
                break
        if mdi is None:
            return False
        subs: List[int] = []
        win32gui.EnumChildWindows(
            mdi, lambda h, _: (subs.append(h), True)[1], None)
        target = None
        for h in subs:
            cls = win32gui.GetClassName(h) or ""
            title = (win32gui.GetWindowText(h) or "").strip()
            if not cls.startswith("Afx:") or not title:
                continue
            if title.lower() in SKIP_TITLES:
                continue
            target = h
            break
        if target is None:
            return False
        win32gui.SendMessageTimeout(mdi, WM_MDIACTIVATE, target, 0,
                                    win32con.SMTO_ABORTIFHUNG, 3000)
        return True
    except Exception:                                         # noqa: BLE001
        return False


def calibrate_equipment_footers(doc) -> List[Dict[str, object]]:
    """按本机SHX实际包围盒补偿底部文字，不能把对齐点当字形中心。"""
    import pythoncom
    from win32com.client import dynamic, VARIANT
    def bounds_center(text):
        lo = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
        hi = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
        returned = text.GetBoundingBox(lo, hi)
        a, b = lo.value, hi.value
        if a is None and isinstance(returned, tuple) and len(returned) == 2:
            a, b = returned
        if a is None or b is None:
            raise RuntimeError('没有取得底部文字原生包围盒')
        return tuple((x+y)/2. for x, y in zip(a, b))
    rows = []
    blocks=dynamic.Dispatch(doc.Blocks._oleobj_)
    for block_index in range(int(blocks.Count)):
        raw=blocks.Item(block_index)
        block = dynamic.Dispatch(raw._oleobj_)
        short_label=str(block.Name).rsplit('$',1)[-1].startswith('EQUIPMENT_LABEL')
        if not short_label and not str(block.Name).rsplit('$',1)[-1].startswith('EQUIPMENT_FOOTER'):
            continue
        texts = [dynamic.Dispatch(block.Item(i)._oleobj_) for i in range(int(block.Count))
                 if block.Item(i).ObjectName == 'AcDbText']
        if not texts:
            continue
        tag_target = (0., 0. if short_label else float(texts[0].Height)+2. if len(texts)>1 else 0., 0.)
        for index, text in enumerate(texts):
            desired = (tag_target[0], tag_target[1]-(float(text.Height)+2. if index else 0.), tag_target[2])
            center = bounds_center(text)
            delta = tuple(a-b for a, b in zip(desired, center))
            point = lambda p: VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, p)
            text.Move(point((0., 0., 0.)), point(delta))
            actual = bounds_center(text)
            if any(abs(a-b) > .001 for a, b in zip(actual, desired)):
                raise RuntimeError('底部文字原生几何中心补偿未通过')
            rows.append({'text': str(text.TextString), 'shift': delta, 'center': actual})
    return rows


def dxf_to_dwg_com(dxf: str, dwg: str, *, create_if_not_exists: bool = False
                   ) -> Dict[str, object]:
    """**默认通道**：用 COM 连到 AutoCAD 打开 DXF 另存为 DWG。返回报告 dict（不抛异常）。

    ⚠️ AutoCAD 忙（模态对话框 / 正在处理命令）会**拒收 COM 调用**，这是环境态不是
    代码错，故对这类失败做有限重试（见 `_is_com_rejected`）；重试前先 `_wake_autocad`
    ——最常见的一种"被拒"是 AutoCAD 停在「开始」选项卡上（永远不静默），
    把活动页切回一张真图纸就好了，这一步能自己救回来。仍失败则把"是不是被拒"
    写进 reason，让用户知道该去 AutoCAD 里处理什么，而不是改代码。
    """
    try:
        app, how = com_session(create_if_not_exists)
    except RuntimeError as exc:
        return {"ok": False, "backend": "com", "reason": str(exc)}
    last: Optional[BaseException] = None
    phase = 'connect'
    waked = False
    for attempt in range(COM_RETRY + 1):
        doc = None
        staging = tempfile.mkdtemp(prefix='chemcad_com_')
        try:
            phase = 'inspect_open_documents'
            from win32com.client import dynamic
            documents=dynamic.Dispatch(app.Documents._oleobj_)
            documents._FlagAsMethod('Open')
            # 不重用用户已经打开的源/目标图，避免覆盖未保存编辑。
            documents._FlagAsMethod('Item')
            owned_paths={os.path.normcase(os.path.abspath(dxf)),os.path.normcase(os.path.abspath(dwg))}
            if any(os.path.normcase(os.path.abspath(str(documents.Item(i).FullName))) in owned_paths for i in range(int(documents.Count))):
                raise RuntimeError('源DXF或目标DWG已在AutoCAD中打开；先保存或关闭该任务文件后重试')
            # 在临时副本中打开/测量/保存，失败不留下半成品目标，也不改输入DXF。
            # COM仍完成全部操作，临时路径使用ASCII名称。
            staged_dxf=os.path.join(staging,'drawing.dxf')
            staged_dwg=os.path.join(staging,'drawing.dwg')
            shutil.copyfile(dxf,staged_dxf)
            phase = 'open_dxf'
            # 获取Item后部分COM包装器方法表会绑定到Document；重新取Documents接口。
            documents=dynamic.Dispatch(app.Documents._oleobj_)
            documents._FlagAsMethod('Open')
            doc = documents.Open(staged_dxf)
            # AutoCAD 2026 的 Open 返回包装器可能仅绑定 Open 名称；
            # 从实际 IAcadDocument 调度接口重绑定，SaveAs/Close 才有正确方法表。
            from win32com.client import dynamic
            doc = dynamic.Dispatch(doc._oleobj_)
            for method in ('SaveAs','Save','Close'):doc._FlagAsMethod(method)
            # 64 = ac2018_dwg；24实际为ac2004_dwg，不能用来声明2018格式。
            phase = 'save_dwg'
            doc.SaveAs(staged_dwg, 64)
            phase = 'calibrate_text'
            footer_centers = calibrate_equipment_footers(doc)
            if footer_centers:
                doc.Save()
            phase = 'close_owned_document'
            doc.Close(False)
            doc = None
            shutil.copyfile(staged_dwg,os.path.abspath(dwg))
            if os.path.isfile(dwg) and os.path.getsize(dwg) > 1024:
                rep = {"ok": True, "backend": "com", "session": how,
                       "bytes": os.path.getsize(dwg)}
                rep['footer_text_centers'] = footer_centers
                if waked:
                    rep["waked"] = "AutoCAD 原本停在「开始」选项卡，已自动切到图纸页"
                return rep
            return {"ok": False, "backend": "com", "reason": "COM 未产出 DWG"}
        except Exception as exc:                              # noqa: BLE001
            last = exc
            if doc is not None:
                try:doc.Close(False)
                except Exception:pass
            if attempt < COM_RETRY and _is_com_rejected(exc):
                waked = _wake_autocad(app) or waked
                time.sleep(COM_RETRY_WAIT)
                continue
            break
        finally:
            checked_staging=os.path.realpath(staging)
            if (os.path.normcase(os.path.dirname(checked_staging))==os.path.normcase(os.path.realpath(tempfile.gettempdir()))
                    and os.path.basename(checked_staging).startswith('chemcad_com_')):
                shutil.rmtree(checked_staging,ignore_errors=True)
    reason = f"{type(last).__name__}: {last}"
    if _is_com_rejected(last):                                # type: ignore[arg-type]
        reason += ("（AutoCAD 正忙或正显示模态对话框——请在 AutoCAD 里处理掉对话框/"
                   "等它空闲后重试；这不是本技能的通道问题）")
        if not waked:
            reason += ("；另一种常见原因是 AutoCAD 停在「开始」选项卡（那样它永远不静默），"
                       "在 AutoCAD 里点一下任一图纸页标即可")
    return {"ok": False, "backend": "com", "reason": reason, "phase": phase}


def require_com(create_if_not_exists: bool = False) -> Dict[str, object]:
    """**前置体检**：能不能连上 AutoCAD。出图前先问一次，别画完才发现没处落。

    返回 `{"ok": bool, "session": …, "progid": …, "reason": …}`。
    """
    try:
        app, how = com_session(create_if_not_exists)
    except RuntimeError as exc:
        return {"ok": False, "progid": ACAD_PROGID, "reason": str(exc)}
    ver = ""
    try:
        ver = str(app.Version)
    except Exception:                                         # noqa: BLE001
        pass
    return {"ok": True, "session": how, "progid": ACAD_PROGID, "version": ver}


#: **默认只走 COM**。要 accoreconsole 必须显式开这个环境变量（逃生口，见模块 docstring）。
def _allow_accore() -> bool:
    return os.environ.get("CHEMCAD_DWG_BACKEND", "").strip().lower() == "accore"


def to_dwg(dxf: str, dwg: str, *, timeout: int = 300,
           create_if_not_exists: bool = False) -> Dict[str, object]:
    """DXF → DWG。**默认只走 COM**；连不上就抛错，绝不静默换通道或给 DXF。"""
    tried: List[Dict[str, object]] = []
    if _allow_accore():
        rep = dxf_to_dwg_accore(dxf, dwg, timeout=timeout)
        if rep.get("ok"):
            return {**rep, "tried": tried + [rep],
                    "escape_hatch": "CHEMCAD_DWG_BACKEND=accore"}
        tried.append(rep)
    rep = dxf_to_dwg_com(dxf, dwg, create_if_not_exists=create_if_not_exists)
    tried.append(rep)
    if rep.get("ok"):
        return {**rep, "tried": tried}
    detail = "；".join(f"{t.get('backend')}: {t.get('reason')}" for t in tried)
    raise RuntimeError(
        "要求输出 DWG，但 COM 通道不可用（本技能强制经 AutoCAD COM 出图）：\n  "
        + detail +
        "\n  解决：① 打开 AutoCAD 后重跑；② 加 --create 让本技能启动 AutoCAD；"
        "③ 确需无界面时 CHEMCAD_DWG_BACKEND=accore；"
        "④ 或 --backend dxf 明确以 DXF 交付。")


def converter_report() -> Dict[str, object]:
    """当前机器上的出图通道体检（供 `--check-tools` / 文档自查用）。

    `com` = COM 通道是否可用（`running` / `created` / 不可用原因）；
    `accoreconsole` = 逃生口是否存在（**不代表默认会用它**）。
    """
    com = require_com(False)
    return {"accoreconsole": find_accoreconsole(),
            "pywin32": "pywin32" not in str(com.get("reason", "")),
            "com": com,
            "default_backend": "com",
            "allow_accore": _allow_accore(),
            "ok": bool(com.get("ok"))}
