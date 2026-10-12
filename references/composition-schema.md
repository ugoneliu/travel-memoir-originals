# 布局与核验接口（version 3）

布局由执行 Skill 的 AI 根据当前照片与描述制作，用户不需要填写。运行时以布局 JSON 所在目录解析相对路径，支持带空格与中文的文件名。只依赖 Python 3.10+、Pillow。

```json
{
  "version": 3,
  "background": "layers/background.png",
  "output": "final.png",
  "report": "composition-report.json",
  "photos": [
    {
      "id": "seaside",
      "path": "inputs/original.jpg",
      "x": 120,
      "y": 600,
      "width": 1100,
      "shape": {"type": "torn", "depth": 7, "step": 6, "seed": 42},
      "border": {"type": "white", "size": 4},
      "protect": {
        "polygons": [[[100, 120], [680, 100], [720, 1100], [80, 1100]]]
      },
      "audit": [
        {"name": "person-01-head", "mask": {"rectangles": [[160, 130, 600, 490]]}}
      ]
    }
  ],
  "foregrounds": [
    {"path": "layers/caption-complete.png", "x": 1300, "y": 700}
  ]
}
```

坐标仅为说明接口，不能直接套到真实照片。真正布局必须使用当前原片的尺寸与内容。

## 背景与照片

`background` 是 PNG，决定最终画布大小。`output` 必须为 PNG；可选 `report` 存合成与像素检查结果。照片数组按从底到顶排序。每个 `id` 唯一；每张有源文件、位置、等比显示宽度、轮廓、保护区与独立核验区。

`x/y` 为整数，表示等比缩放后照片左上角。`width` 为正整数，高度由 EXIF 转正后原片比例计算。不要给第二个目标高度；不支持旋转、滤镜、透视或 AI 修复。所有保护与核验位置必须在画布内，其他安全背景可出画布。

`shape.type`：

- `rect`：保留直边，适合有意设计的完整照片纸片。
- `torn`：细碎撕纸，`depth`、`step` 为最终渲染像素，`seed` 控制重复性。不同尺寸应调整幅度；算法不能自动保证美感。
- `custom`：例如 `{"type":"custom","alpha":{"path":"masks/photo-alpha.png"}}`，指向与 EXIF 转正后原片同尺寸的灰度 L PNG。0 隐藏，255 原片完全可见；半透明只用于安全边缘。

`border` 为可选，`{"type":"white","size":4}` 向轮廓外扩白边；省略或 `{"type":"none","size":0}` 表示无边。后照片及白边不得覆盖前照片保护区。

## 源坐标蒙版

`protect` 与 `audit[].mask` 可以采用两种形式：

```json
{"path": "masks/person-protect.png"}
```

文件必须为同 EXIF 转正后原片尺寸的二值灰度 L PNG，白色为选中区域。也可在 JSON 定义：

```json
{
  "rectangles": [[160, 130, 600, 490]],
  "polygons": [[[100, 100], [300, 100], [330, 600], [80, 600]]]
}
```

矩形按左、上、右、下给出；右、下为不包含边界的坐标。多边形按顶点顺序排列，采用 Pillow 填充。坐标在原片转正后的尺寸中，不是画布坐标。必须查看原片确认内容和留量，不能复制案例中的数字。

`protect` 服务于剪切和遮挡保护；`audit` 是另外从原片定义的核验清单，每项有明确名字和范围，至少覆盖每个人的脸与头部。两者职责不同，核验不能通过最终 alpha 筛掉被覆盖的像素。无人照片仍核验重要场景或物件，不伪造人脸审计。

## 前景与完整纸片

`foregrounds` 可省略。每项 `path` 是透明 RGBA PNG，`x/y` 为整数，不再缩放。先按布局生成或准备正确尺寸；图片含实际文字纸片、完整纸边和需要一起显示的装饰。按数组顺序从底到顶放在照片之上。任何非零 alpha 像素都不得触碰人物保护区；这能阻止轻微半透明白雾或装饰侵入脸部。

前景之间的遮挡是设计行为，脚本不知道哪个爱心应该可见；看图检查，必要时将文字、心形及其完整纸底放在同一层中，或调整层序。

## 命令与失败行为

```sh
python scripts/compose.py /absolute/path/to/layout.json
python scripts/verify.py /absolute/path/to/layout.json --image /absolute/path/to/final.png --report /absolute/path/to/final-audit.json
```

`verify.py` 未给 `--image` 时使用布局的 `output`。核验重新打开原片和最终保存 PNG，使用同一规定的 EXIF/RGB/LANCZOS 等比变换生成同尺寸参照，再比较完整保护与核验区域。报告记录源文件 SHA256、尺寸与变换，以及成品 SHA256；不是人脸识别或美学评分。

配置错误、遮挡冲突、越界或像素不一致时退出码为 2。不能覆盖输入照片、蒙版、背景、前景或布局，包含软/硬链接别名。成功报告不能替代最终视觉检查。对于已存在的成品或报告，选择新文件名可保留历史版本。
