from pydantic import BaseModel, ConfigDict
from typing import Any, Optional


class Coordinate(BaseModel):
    lat: float
    lon: float


class DeliveryJob(BaseModel):
    # Airtable record id (you confirmed record id == delivery id)
    id: Any

    # Optional business code if you add later
    delivery_code: Optional[str] = None

    job: Optional[str] = None
    job_id: Optional[Any] = None
    job_name: str = ""
    job_address: str = ""
    sales_order: str = ""
    packing_slip: str = ""
    driver: str = ""

    door: int = 0
    frame: int = 0
    exp_frame: int = 0
    med_skid: int = 0
    box: int = 0
    small_skid: int = 0
    large_skid: int = 0
    pickup: bool = False
    requires_empty_truck: bool = False

    lat: float = 0.0
    lon: float = 0.0
    matrix_index: Optional[int] = None

    # Optional future time window fields
    window_start: Optional[str] = None
    window_end: Optional[str] = None

    @property
    def requires_truck(self) -> bool:
        return (
            (self.door or 0) > 0
            or (self.frame or 0) > 0
            or (self.exp_frame or 0) > 0
            or (self.med_skid or 0) > 0
            or (self.small_skid or 0) > 0
            or (self.large_skid or 0) > 0
            or bool(self.pickup)
        )

    model_config = ConfigDict(coerce_numbers_to_str=True, extra="ignore")
