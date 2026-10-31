"""Static spending controls and pricing configuration."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gateway.cost.pricing import Price, PriceCatalog


class SpendingLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_micro_usd: int | None = Field(default=None, ge=0)
    monthly_micro_usd: int | None = Field(default=None, ge=0)


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    pricing: list[Price] = Field(default_factory=list)
    default_key_limits: SpendingLimits = Field(default_factory=SpendingLimits)
    key_limits: dict[str, SpendingLimits] = Field(default_factory=dict)
    principal_teams: dict[str, str] = Field(default_factory=dict)
    team_limits: dict[str, SpendingLimits] = Field(default_factory=dict)
    team_readers: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> "CostConfig":
        PriceCatalog(self.pricing)
        if not self.enabled and (self.pricing or self.team_limits):
            raise ValueError("pricing and team limits require cost.enabled = true")
        for principal, team in self.principal_teams.items():
            if team not in self.team_limits:
                raise ValueError(f"team {team!r} for {principal!r} has no configured limits")
        for team in self.team_readers:
            if team not in self.team_limits:
                raise ValueError(f"team readers reference unknown team {team!r}")
        return self

    def limits_for(self, principal_id: str) -> list[tuple[str, str, SpendingLimits]]:
        limits = [("key", principal_id, self.key_limits.get(principal_id, self.default_key_limits))]
        if team := self.principal_teams.get(principal_id):
            limits.append(("team", team, self.team_limits[team]))
        return limits
