"""AITube-PLLM 配置管理"""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """应用配置"""

    # 数据库配置
    database_url: str = Field(
        default="postgresql://postgres:postgres@postgres:5432/aitube_pllm",
        description="PostgreSQL 连接字符串",
    )
    database_pool_min: int = Field(default=5, description="连接池最小连接数")
    database_pool_max: int = Field(default=20, description="连接池最大连接数")

    # 应用配置
    app_name: str = Field(default="AITube-PLLM", description="应用名称")
    debug: bool = Field(default=False, description="调试模式")
    log_level: str = Field(default="INFO", description="日志级别")

    # LiteLLM 配置
    litellm_api_base: str = Field(
        default="http://litellm:4000",
        description="LiteLLM API 基础 URL",
    )
    litellm_master_key: str = Field(
        default="",
        description="LiteLLM master key (从环境变量或密钥管理服务获取)",
    )

    # 签名验证配置
    signature_timestamp_window: int = Field(
        default=300,
        description="签名时间戳允许窗口(秒)",
    )
    audience: str = Field(
        default="aitube-pllm",
        description="本系统的 audience 标识符",
    )

    # 速率限制默认值
    default_rate_limit_rpm: int | None = Field(
        default=None,
        description="默认速率限制(每分钟请求数), None 表示无限制",
    )

    # 仪表盘只读接口 token（用于外部前端拉取监控聚合数据）
    dashboard_token: str = Field(
        default="",
        description="只读仪表盘 token，配合 Authorization: Bearer 或 X-Dashboard-Token 使用",
    )

    # 全局强制模型（单模型部署 / 客户机器）：设置后忽略客户端 model，统一路由到此模型。
    # 值须为 LiteLLM 中已配置的模型别名，且须在 PLLM models 表中 is_current & is_enabled。
    # 详见 docs/global-model-override-proposal.md。对应环境变量 PLLM_FORCE_MODEL。
    force_model: str | None = Field(
        default=None,
        description="全局强制模型：设置后忽略客户端 model，统一路由到此模型",
    )

    # 缓存配置
    models_cache_ttl: int = Field(
        default=30,
        description="模型列表缓存 TTL(秒)",
    )

    model_config = {
        "env_prefix": "PLLM_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


settings = Settings()
