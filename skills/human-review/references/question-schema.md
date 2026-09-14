# 题目契约

```json
{
  "id": "color-theme",
  "round": 1,
  "remaining": 1,
  "type": "single",
  "question": "演示页面使用哪种配色？",
  "description": "虚构界面偏好演示，不修改实际设置。",
  "context": "假设你希望在较暗的环境中查看一个示例页面。当前只选择配色。",
  "impact": "- **深色**：使用较暗的背景。\n- **浅色**：使用较亮的背景。",
  "scenario": "打开虚构的示例页面，比较两种背景下的阅读感受。",
  "recommendation": "**建议深色。** 前提是你偏好较暗的背景；也可以选择浅色。",
  "evidence": "仅为虚构的界面偏好示例，不包含真实用户记录。",
  "table": {
    "headers": [
      "比较项",
      "深色",
      "浅色"
    ],
    "rows": [
      [
        "背景",
        "较暗",
        "较亮"
      ],
      [
        "本轮范围",
        "仅配色",
        "仅配色"
      ]
    ]
  },
  "options": [
    {
      "id": "dark",
      "label": "深色",
      "description": "使用较暗的背景。",
      "recommended": true
    },
    {
      "id": "light",
      "label": "浅色",
      "description": "使用较亮的背景。"
    }
  ]
}
```

`type` 可选 single（默认）、multi、text；text 可省略 options。无选项时必须填写自由意见。选项 id 必须唯一。

`context/impact/scenario/recommendation/evidence/options[].description` 支持安全 Markdown 子集：段落、换行、标题、列表、粗体、行内代码和 fenced code。表格用结构化 `table`，不支持 Markdown 表格语法、外链图片或任意 HTML。

`remaining` 必须为正整数；`round` 表示实际审核轮次。`revision` 由服务分配，每次发布递增；提交携带该 revision，拒绝重复或过期提交。推荐只显示标记，页面不预选。
