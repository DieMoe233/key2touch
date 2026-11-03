<div align="center">

<img src="data/io.github.shorin_kiwata.key2touch.svg" width="112" alt="key2touch">

# key2touch

将键盘按键映射为触屏操作，适用于 Wayland 桌面，支持 Waydroid 游玩。

<img src="docs/hud.png" width="760" alt="游戏中的 HUD">

</div>

## 特性

- **低延迟**：直接读写内核 evdev / uinput，按键到触摸 < 0.1 ms；引擎以实时优先级运行，满载时也不被游戏挤占
- **多点触控**：点击、WASD 摇杆、组合键（Ctrl / Alt / Shift / Super），最多 10 指同时按下
- **鼠标视角**：鼠标移动拖动视角（原神这类游戏），鼠标按键也能绑定；按住 Alt 临时显示鼠标
- **可视化编辑**：在游戏画面上直接点击放置按键，拖动、滚轮缩放、属性面板
- **跟随窗口**：只在指定窗口（如 Waydroid）获得焦点时生效，其他时候键盘照常使用
- **不怕缩放**：坐标按显示器比例保存，分数缩放、多显示器都不需要换算
- **托盘常驻**：关闭窗口后在后台继续运行

<p align="center">
  <img src="docs/editor.png" width="760" alt="布局编辑器"><br>
  <img src="docs/main.png" width="300" alt="主界面">
  <img src="docs/adv.png" width="300" alt="高级设置">
</p>

## 安装

```sh
yay -S key2touch-git
```

用户需要在 `input` 组里：`sudo usermod -aG input $USER`，然后重新登录。

<details>
<summary>从源码运行</summary>

依赖：`python-evdev` `python-gobject` `python-cairo` `gtk4` `libadwaita` `gtk4-layer-shell`

```sh
git clone https://github.com/SHORiN-KiWATA/key2touch
key2touch/bin/key2touch
```
</details>

## 使用

1. **编辑布局**：在画面上点击要触摸的位置，按下对应的键（也可以点鼠标按键）；`＋ 摇杆` 添加方向摇杆，`＋ 视角` 添加鼠标视角区域
2. **启用映射**：打开开关，或在任意窗口按开关热键（默认 F12）
3. **选择显示器**：选游戏窗口所在的那一块

在游戏里按 **Shift+F12** 直接打开编辑器，再按一次保存并退出。建议 Waydroid 全屏运行，按键点相对整个显示器定位。

## 命令行

```sh
key2touch on | off | toggle          # 开关映射（适合绑定到桌面快捷键）
key2touch status --json              # 当前状态
key2touch profile use 原神            # 切换配置
key2touch point add tap j 85% 78%    # 不开界面直接添加按键点
key2touch point add stick 15% 70% --keys w,a,s,d
key2touch set edit-hotkey ctrl+e     # 修改编辑热键
key2touch windows                    # 游戏窗口在哪个显示器上
key2touch check                      # 自检：越界、重叠、显示器不对、权限…
```

所有命令都支持 `--json`，退出码固定（0 成功 / 1 错误 / 2 参数错误 / 3 未运行）。
**AI agent** 可以直接读 `key2touch agent-help` 学会用法：看一眼游戏截图，就能把布局写出来。

## 桌面支持

| 桌面 | 跟随窗口焦点 | 多显示器触摸映射 | HUD |
|---|:---:|---|:---:|
| niri | ✅ | 首次使用按提示在配置里加一行 | ✅ |
| Hyprland / Sway（未实测） | ✅ | 自动 | ✅ |
| KDE Plasma 等 | — | 系统设置 → 触摸屏 | ✅ |
| GNOME | — | 系统设置 | — |

## 原理

```
键盘 ─evdev→ key2touch 引擎 ─uinput→ 虚拟触摸屏 → libinput → 合成器 → wl_touch → 游戏
```

## 致谢

- [Kuimuztion/pjsk-waydroid-controller](https://github.com/Kuimuztion/pjsk-waydroid-controller)：基于 evdev / uinput 的 PJSK + Waydroid 触控映射引擎与 HUD，同样的思路

## 许可证

[GPL-3.0-or-later](LICENSE)
