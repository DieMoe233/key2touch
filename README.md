# key2touch

将键盘按键映射为触屏操作，适用于 Wayland 桌面，支持 Waydroid 游玩。

键盘 → 虚拟多点触摸屏映射，任何支持触摸的 Wayland 程序都能用（Waydroid、浏览器、GTK/Qt 应用……）。
核心（evdev → uinput 触摸屏）与桌面无关；桌面相关的部分在 `key2touch/desktop/` 里按后端实现。

```
键盘 evdev ─▶ 引擎进程（独立 GIL，select 循环）─▶ /dev/uinput MT-B 触摸屏
                                                  └▶ libinput ▶ niri ▶ wl_touch ▶ Waydroid
GUI 进程（GTK4）：控制窗口 + layer-shell 编辑器 / 点击穿透 HUD
```

## 运行

```sh
bin/key2touch [配置名]        # 或 python -m key2touch
```

依赖：python-evdev、PyGObject + GTK 4 + libadwaita、gtk4-layer-shell、pycairo；用户需在 `input` 组（`/dev/uinput` 可写）。
gtk4-layer-shell 需要 LD_PRELOAD，程序会自动 re-exec 补上。

## 文件位置

> 本项目原名 waymap。首次启动时会把 `~/.config/waymap` 自动搬到 `~/.config/key2touch`；
> niri 的 include 行指向旧文件时，控制窗口会提示「迁移」。

| 内容 | 位置 |
|---|---|
| 配置（每个游戏一个 JSON） | `~/.config/key2touch/profiles/<名字>.json` |
| 全局设置（上次配置、HUD） | `~/.config/key2touch/settings.json` |
| niri 触摸映射（仅 niri，自动生成） | `~/.config/key2touch/niri-touch.kdl` |
| 引擎日志 | `~/.cache/key2touch/engine.log` |
| Python 字节码（用 `bin/key2touch` 启动时） | `~/.cache/key2touch/pycache/` |

## 桌面支持

启动时自动检测（可用 `KEY2TOUCH_DESKTOP=niri|hyprland|sway|generic` 强制指定）：

| 桌面 | 焦点跟随 | 触摸映射到指定输出 | HUD / 编辑器 |
|---|---|---|---|
| niri | ✅ IPC 事件流 | config.kdl 一行 include（见下） | ✅ layer-shell |
| Hyprland | ✅ socket2 事件 | ✅ 运行时 `hyprctl keyword device[…]:output`，只影响虚拟设备，不改配置 | ✅ layer-shell |
| Sway | ✅ i3-ipc 订阅 | ✅ 运行时 `input "4617:1:key2touch_virtual_touchscreen" map_to_output`，不改配置 | ✅ layer-shell |
| KDE Plasma 等（generic） | ❌ 用开关热键手动启停 | 单屏无需设置；多屏在系统设置的触摸屏页面选目标显示器 | ✅ layer-shell |
| GNOME（generic） | ❌ 同上 | 同上（gsettings 按设备设置） | 编辑器退化为全屏窗口；HUD 不可用（Mutter 不支持 layer-shell） |

输出列表来自 GDK（逻辑尺寸），所有桌面通用。Hyprland / Sway 后端用模拟 IPC 服务器做了协议测试，未在真实桌面上验证。

### niri

niri 把**所有**触摸设备映射到 `input.touch.map-to-output`（未设置则是第一个输出），既没有按设备的映射，也没有 IPC，
所以 `~/.config/niri/config.kdl` 末尾必须有一行（控制窗口里点「写入 / 迁移」即可）：

```kdl
include optional=true "/home/<你>/.config/key2touch/niri-touch.kdl"
```

被引用的文件放在 key2touch 自己的目录里，切换输出时自动改写，niri 热重载。include 必须在最后（niri 的 include 是位置相关的）。

## 坐标

配置里所有位置都是相对输出的 0..1 归一化坐标。niri 按 `归一化值 × 输出逻辑尺寸 + 输出偏移` 换算触摸位置，
HUD 的 layer surface 也正好是输出的逻辑矩形，所以分数缩放（1.3x 等）不需要任何像素换算。
摇杆半径以输出高度为单位，x 方向按宽高比修正，保证屏幕上是圆的。

## 编辑布局

- 顶栏：切换配置（切换前自动保存）、新建配置、＋摇杆、清空、放弃修改、保存并退出
- 左键空白处添加按键点，然后按下要绑定的键；可以先按住 Ctrl/Alt/Shift/Super 绑组合键，单独按修饰键再松开则绑修饰键本身
- 点击已有点：重新绑定；拖动：移动；滚轮：调整大小（按键点和摇杆都可以）；右键 / Delete：删除；Esc：保存并退出
- 选中后右侧属性面板可以改类型（点击 / 摇杆）、逐个方向绑定、调大小、删除

同一个键（组合）只能驱动一个控件，重复绑定会从旧控件上摘掉。

## 行为

- 「仅在匹配的窗口获得焦点时生效」（默认关闭，任何支持 wl_touch 的程序都能用）：通过 niri IPC 事件流跟踪焦点，app_id 匹配正则（默认 `(?i)waydroid`）才生效，失焦时立刻抬起所有触点。
- 「独占键盘」：生效时 EVIOCGRAB 键盘，已映射的键只变成触摸，其余键经虚拟键盘透传（niri 快捷键照常用）。
  只在该键盘没有按住的键时才 grab，避免切焦点时按着的 Super 等键卡住。
- 开关热键（默认 F12）随时启停。引擎直接读内核 evdev，所以热键在任何窗口下都有效（不经过 Wayland 协议）。
- 引擎通过 rtkit 申请实时调度（SCHED_RR 10，不需要 root），并冻结 GC，CPU 满载时也不会被游戏挤占。

## 测试

```sh
python -m unittest
```

实测（本机，按键到触摸帧写出）：空闲 中位 0.12 ms；12 核满载 + 实时调度 中位 0.05 ms，p99 0.39 ms。

## 已知限制

- 坐标相对整个输出，不跟随窗口；建议 Waydroid 全屏或固定位置。
- 仅支持 transform Normal 的输出。
- 暂无鼠标视角（mouse look）映射。
