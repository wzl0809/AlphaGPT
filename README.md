# AlphaGPT · AI 量化因子研究平台

> 基于 Transformer 策略网络 + PPO 强化学习的 A 股量化因子挖掘与回测研究工具。


---

## 这是什么

AlphaGPT 是一个**客户端运算、服务端协调**的量化研究平台。核心引擎用强化学习（Transformer 策略网络 + 近端策略优化 PPO）在 A 股历史数据上**自动挖掘数学因子公式**——由 19 个候选因子与 12 个算子组合而成的波兰式表达式，例如 `DECAY(ADD(WR_DIFF, VOL_RET_CORR))`——并以样本外夏普、最大回撤、胜率为目标持续优化。全部训练与回测运算在用户本机完成；服务端只负责账户、同步与统计，不做任何高负载运算。


---

## 核心能力

- 🧠 **因子挖掘引擎** —— 强化学习自动生成并筛选因子公式；内置 19 因子（动量 / 量价 / 趋势波动 / 超买超卖 / 资金流）× 12 算子（ADD / SUB / MUL / GATE / DECAY …）。
- 📊 **样本外回测** —— 训练 / 验证 / 测试三段切分，`final_reality_check` 在样本外数据上诚实评估（夏普、年化、最大回撤、Calmar、样本外胜率），并披露引擎自身的乐观偏差。
- 📚 **本地公式库** —— 训练成果由本地 SQLite 管理，可按夏普 / 回撤 / 年化排序与筛选；公式来源限于系统训练产出，确保均经过回测验证。
- 📈 **量化研究信号** —— 对自选公式基于历史数据生成研究信号与策略净值回测曲线；信号圆圈可显示因子末值（纯数值）或研究方向文字。
- 🤖 **AI 研究仪表盘（可选）** —— 接入 DeepSeek / GLM / 通义 / OpenAI 兼容大模型 + Tavily 新闻，输出结构化研究观察（客观技术面、数据视角、信号归因等，研究参考口径）。
- 🌐 **多数据源回退** —— 新浪 → akshare → baostock → tushare，本地 parquet 缓存，命中即零网络。
- 🖥️ **硬件识别** —— 自动识别 CPU / GPU / 内存；有 NVIDIA 显卡时训练走 CUDA，否则走 CPU。
- 🔁 **断点续训与产物清理** —— 训练断点可续；系统设置内置 reports / checkpoints 清理页，防磁盘占用膨胀。

---

## 获取与运行

### ① 下载开箱即用版（推荐，无需自配环境）

到本仓库的 **Releases** 页下载对应你机器的压缩包并解压：

| 包 | 体积 | 适用 |
|---|---|---|
| `AlphaGPT-Client-x.x.x-win64-cpu.zip` | ~200 MB | 所有 Windows，CPU 版 torch |
| `AlphaGPT-Client-x.x.x-win64-cuda.zip` | ~2 GB | 有 NVIDIA 显卡的 Windows，CUDA 版 torch，训练更快 |

解压后双击 **`run.bat`**（带控制台）或 **`start.vbs`**（无窗口后台）即可启动。

### ② 克隆本仓库自行配置（需自备 Python 3.12）

```bash
git clone https://github.com/wzl0809/AlphaGPT.git
cd AlphaGPT
pip install -r requirements.txt
pythonw run.py        # 或双击 run.bat
```

首次启动进入登录 / 注册（连接官方服务端获得账户）；在「系统设置」填入可选的 DeepSeek / Tavily API Key 后，即可启用 AI 研究仪表盘。

---

## 技术栈

| 层 | 技术 |
|---|---|
| 研究引擎 | Transformer 策略网络 + PPO 强化学习 + TorchScript 算子（PyTorch） |
| 客户端 | Flask + Flask-SocketIO（threading / polling-only）、SQLite + SQLAlchemy |
| 数据源 | 新浪 / akshare / baostock / tushare（本地 parquet 缓存） |
| AI（可选） | DeepSeek / GLM / 通义 / OpenAI 兼容 + Tavily |
| 界面 | 原生 HTML / CSS / JS，苹果磨砂质感，深 / 浅双主题 |

---

## 合规与使用条款

- 本平台为**量化研究 / 回测学习工具**，所有输出均为基于公开历史数据的研究回显，**不构成投资建议或买卖指令**。
- 本平台**不提供证券投资咨询、不代客决策、不含真实交易 / 下单功能**（界面中的 QMT 仅为接口占位演示）。
- 使用本软件即表示你理解上述定位，并独立承担据此研究的全部风险与后果。

> ⚠️ **风险提示与免责声明**
> 本平台所有公式、信号、评分、回测与 AI 评述，均为基于公开历史数据的算法 / 模型研究回显，仅供量化研究学习，**不构成任何投资建议、证券推荐或买卖指令**，不代表未来走势。市场有风险，决策请独立判断、自负盈亏。

---

## 联系

- 邮箱：**wzl0809@gmail.com**（唯一官方联系入口；软件定制服务同样由此咨询）

---

## License

专有软件（Proprietary）。本仓库编译产物可供个人免费用于量化研究学习。详细条款与商业授权需求请通过上方邮箱联系。
