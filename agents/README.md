# Agent 安装目录

这里预留给用户安装或开发中的 Agent。

当前阶段的后端只扫描下面这种结构中的 `manifest.yaml`，不会导入或执行插件代码：

```text
agents/
└─ my_agent/
   └─ manifest.yaml
```

内置 Agent 位于 `backend/app/agents/builtin/`，用户目录中的同名 Agent 暂时不会覆盖内置 Agent。

