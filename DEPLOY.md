# 部署与扣子连接

## Render

1. 把本目录上传到一个新的 GitHub 仓库。
2. Render 中选择 **New > Blueprint**，连接该仓库；或者选择 **New > Web Service**。
3. 如果手动创建 Web Service：
   - Runtime：Python
   - Build Command：`pip install -r requirements.txt`
   - Start Command：`gunicorn --bind 0.0.0.0:$PORT app:app`
4. 部署完成后访问 `/health`，确认返回 `status: ok`。
5. 在 Render 的 Environment 页面复制 `REPORT_API_KEY` 的值，供扣子 HTTP 节点使用。不要公开这个值。

## 扣子 HTTP 节点

- Method：`POST`
- URL：`https://你的服务.onrender.com/generate-report`
- Header：`Content-Type: application/json`
- Header：`X-API-Key: 你的 REPORT_API_KEY`
- Body：JSON

```json
{
  "姓名": "开始节点的 child_name",
  "年龄": "开始节点的 age",
  "性别": "开始节点的 gender",
  "在读年级": "开始节点的 grade",
  "测评日期": "开始节点的 test_date",
  "绘画主题": "树木画",
  "画面观察": "整理专家结论节点的 observation",
  "专业结论": "整理专家结论节点的 conclusion",
  "家长关注重点": "整理专家结论节点的 parent_focus",
  "沟通建议": "整理专家结论节点的 advice"
}
```

以上引号中的变量值必须用扣子的变量选择器插入，不要把示例文字原样发送。

HTTP 节点成功后会返回：

```json
{
  "success": true,
  "report_url": "https://你的服务.onrender.com/reports/xxx.docx",
  "file_name": "drawing-report-xxx.docx"
}
```

结束节点引用 HTTP 节点返回的 `report_url` 即可。

## 当前文件保存方式

生成的文件暂存在 Render 实例中，默认 24 小时后清理；实例重启或重新部署时也可能消失。适合第一版测试和即时下载。正式交付时，再把生成后的 DOCX 上传到飞书云盘或对象存储，并返回长期链接。
