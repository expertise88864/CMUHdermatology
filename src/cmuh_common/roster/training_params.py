"""Validated teaching quotas shared by settings, solvers and reporting."""
from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class TrainingParams:
    clerk_low: int = 9
    clerk_target: int = 10
    clerk_high: int = 11
    pgy_low: int = 1
    pgy_target: int = 2
    pgy_high: int = 2
    external_low: int = 30
    external_target: int = 50
    external_high: int = 70
    family_low: int = 30
    family_target: int = 50
    family_high: int = 70
    clerk_biopsy_low: int = 1
    clerk_biopsy_high: int = 2
    external_biopsy: int = 1
    family_biopsy: int = 1

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            limit = (100 if f.name.startswith(("external_", "family_"))
                     and "biopsy" not in f.name else
                     20 if f.name.startswith("clerk_") and "biopsy" not in f.name else 10)
            if type(value) is not int or not 0 <= value <= limit:
                raise ValueError(f"{f.name} must be an integer from 0 to {limit}")
        for scope in ("clerk", "pgy", "external", "family"):
            low, target, high = self.follow_band(scope)
            if not low <= target <= high:
                raise ValueError(f"{scope}: minimum <= target <= maximum is required")
        if self.clerk_biopsy_low > self.clerk_biopsy_high:
            raise ValueError("clerk biopsy: minimum must not exceed maximum")

    @classmethod
    def from_config(cls, cfg: dict) -> "TrainingParams":
        raw = cfg.get("training_params", {})
        if not isinstance(raw, dict):
            raise ValueError("training_params must be an object")
        return cls(**{f.name: raw[f.name] for f in fields(cls) if f.name in raw})

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    def follow_band(self, scope: str) -> tuple[int, int, int]:
        return (getattr(self, f"{scope}_low"), getattr(self, f"{scope}_target"),
                getattr(self, f"{scope}_high"))

    def clerk_biopsy_quota(self, slots: int, people: int) -> int:
        return min(self.clerk_biopsy_high,
                   max(self.clerk_biopsy_low, slots // people if people else 0))
