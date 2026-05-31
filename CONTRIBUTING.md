# 贡献指南

感谢你对家庭现金流雷达项目的关注！本文档将帮助你了解如何参与项目开发。

## 目录

- [报告 Bug](#报告-bug)
- [提交功能请求](#提交功能请求)
- [提交代码](#提交代码)
- [代码规范](#代码规范)
- [提交信息规范](#提交信息规范)

## 报告 Bug

使用 [GitHub Issues](https://github.com/Robs87/family-cashflow-radar/issues) 报告 Bug，请包含：

- 清晰的标题和描述
- 复现步骤
- 期望行为与实际行为
- 运行环境（OS、Python 版本等）
- 相关日志输出

## 提交功能请求

使用 [GitHub Issues](https://github.com/Robs87/family-cashflow-radar/issues) 提交功能请求，请说明：

- 你希望解决的问题
- 期望的解决方案
- 替代方案（如有）

## 提交代码

### 开发流程

1. **Fork 仓库**：点击页面右上角的 Fork 按钮
2. **克隆到本地**：
   ```bash
   git clone https://github.com/your-username/family-cashflow-radar.git
   cd family-cashflow-radar
   ```
3. **创建特性分支**：
   ```bash
   git checkout -b feature/your-feature
   ```
4. **开发并测试**：
   ```bash
   # 运行测试
   pytest tests/
   
   # 运行特定测试
   pytest tests/test_classify.py
   ```
5. **提交变更**：
   ```bash
   git add .
   git commit -m 'feat: add your feature'
   ```
6. **推送分支**：
   ```bash
   git push origin feature/your-feature
   ```
7. **创建 Pull Request**：在 GitHub 页面点击 "New Pull Request"

### Pull Request 规范

- 标题清晰描述变更内容
- 描述中说明变更原因和实现方式
- 关联相关 Issue（如有）
- 确保所有测试通过
- 更新相关文档（如适用）

## 代码规范

### Python

- 遵循 [PEP 8](https://peps.python.org/pep-0008/) 代码风格
- 使用类型注解（Type Hints）
- 函数和类必须有文档字符串
- 金额使用整数分（`amount_cents`），禁止使用浮点数

### SQL

- 关键字大写（`SELECT`, `INSERT`, `WHERE`）
- 表名和列名使用蛇形命名法（snake_case）
- 必须有适当的索引

### 测试

- 所有新功能必须有测试覆盖
- 测试使用合成数据，不使用真实账本
- 测试文件放在 `tests/` 目录下

## 提交信息规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### 类型（Type）

- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 代码格式调整（不影响逻辑）
- `refactor`: 代码重构
- `perf`: 性能优化
- `test`: 测试相关
- `chore`: 构建/工具/辅助变更

### 示例

```
feat(classify): 添加投资流入分类规则

- 支持基金赎回、股票卖出等投资流入识别
- 与投资流出规则保持一致的优先级

Closes #123
```

```
fix(monthly): 修复月度现金流重复计算问题

- 使用 UNIQUE 约束防止重复记录
- 添加幂等性测试用例
```

## 开发环境设置

```bash
# 克隆仓库
git clone https://github.com/Robs87/family-cashflow-radar.git
cd family-cashflow-radar

# 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 安装依赖（如有）
pip install -r requirements.txt

# 运行测试
pytest tests/
```

## 问题反馈

如有任何问题，欢迎通过 [GitHub Issues](https://github.com/Robs87/family-cashflow-radar/issues) 反馈。

---

感谢你的贡献！🎉
