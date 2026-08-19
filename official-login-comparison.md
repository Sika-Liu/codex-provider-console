# 官方登录对比

## 对比对象

1. Codex++ 官方登录档案：`C:\Users\85015\AppData\Local\Temp\codex-clipboard-e5669f81-c4a2-46c6-ac3b-d545c0c02a54.png`
2. 云端控制台官方登录表单：`C:\Users\85015\AppData\Local\Temp\codex-clipboard-1bcc2fc8-3bd8-42a1-9a6b-9d76babdd888.png`

## 结论

两者都使用已有 ChatGPT/Codex 登录态，而不是 API Key。Codex++ 直接将当前 `~/.codex/auth.json` 读入该档案；云端控制台的后端也具备相同的“捕获认证快照”能力，但当前页面没有暴露操作入口，因此无法从表单完成官方登录档案的捕获与激活。

## 主要差异

- Codex++：官方登录时显示“混入 API KEY”的可选项；云端版当前没有此选项。
- Codex++：在详情中直接呈现当前 `auth.json` 内容；云端版只显示已掩码的认证预览。后者更适合云端控制台，因为页面展示完整令牌会泄露长期访问凭据。
- Codex++：官方登录表单不要求 Base URL 或 API Key；云端版已按这一点隐藏 Base URL/Key，后端也接受空 Base URL。
- Codex++：显示“使用中”状态；云端版保存后可以“设为当前”，但必须先捕获认证快照。
- 云端版额外提供模型列表、通用配置、会话标签迁移；这些不是官方登录必须项。

## 必须补齐

在官方登录模式下增加“捕获当前官方登录”按钮，调用现有的 `POST /api/providers/{id}/capture-auth`。应在保存档案后可用，并明确说明：它仅从服务器的 `/home/ubuntu/.codex/auth.json` 创建私有快照，页面不显示 token。

## 安全提醒

参考图中的 `auth.json` 含有可用认证令牌。不要继续分享原图或令牌文本；如该截图曾发到不可信渠道，应在 ChatGPT/Codex 中重新登录以轮换凭据。
