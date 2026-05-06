from .rotation import (
    PriceEngineConfig,
    UniverseThresholds,
    build_short_term_plan,
    build_swing_plan,
    create_price_engine,
    create_reviewer_agent,
    create_sector_rotation_agent,
    create_universe_agent,
)

__all__ = [
    "PriceEngineConfig",
    "UniverseThresholds",
    "build_short_term_plan",
    "build_swing_plan",
    "create_price_engine",
    "create_reviewer_agent",
    "create_sector_rotation_agent",
    "create_universe_agent",
]

try:
    from .utils.agent_utils import create_msg_delete
    from .utils.agent_states import AgentState, InvestDebateState, RiskDebateState

    from .analysts.fundamentals_analyst import create_fundamentals_analyst
    from .analysts.market_analyst import create_market_analyst
    from .analysts.news_analyst import create_news_analyst
    from .analysts.social_media_analyst import create_social_media_analyst
    from .researchers.bear_researcher import create_bear_researcher
    from .researchers.bull_researcher import create_bull_researcher
    from .risk_mgmt.aggressive_debator import create_aggressive_debator
    from .risk_mgmt.conservative_debator import create_conservative_debator
    from .risk_mgmt.neutral_debator import create_neutral_debator
    from .managers.research_manager import create_research_manager
    from .managers.portfolio_manager import create_portfolio_manager
    from .trader.trader import create_trader

    __all__.extend(
        [
            "AgentState",
            "InvestDebateState",
            "RiskDebateState",
            "create_msg_delete",
            "create_bear_researcher",
            "create_bull_researcher",
            "create_research_manager",
            "create_fundamentals_analyst",
            "create_market_analyst",
            "create_neutral_debator",
            "create_news_analyst",
            "create_aggressive_debator",
            "create_portfolio_manager",
            "create_conservative_debator",
            "create_social_media_analyst",
            "create_trader",
        ]
    )
except ModuleNotFoundError:
    # Lightweight CLI/tests only need rotation modules. The full upstream graph
    # becomes available once langgraph/langchain dependencies are installed.
    pass
