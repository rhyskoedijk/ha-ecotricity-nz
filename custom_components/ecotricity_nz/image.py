"""Image entity showing the plan's time-of-use chart from Ecotricity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.util import dt as dt_util

from .api.onboarding import ONBOARDING_URL
from .api.timeframes import chart_path, needs_season
from .const import CONF_INCLUDE_GST, DEFAULT_INCLUDE_GST
from .coordinator import PricingCoordinator
from .entity import EcotricityEntity
from .tariff import WEEKDAY_COUNT

if TYPE_CHECKING:
    from datetime import tzinfo

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import EcotricityConfigEntry

# The image only reads coordinator data, so there's no need to limit parallel updates.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcotricityConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the time-of-use chart image."""
    runtime = entry.runtime_data
    async_add_entities(
        [TimeOfUseChartImage(hass, runtime.pricing, runtime.pricing.icp, runtime.local_tz)]
    )


class TimeOfUseChartImage(EcotricityEntity[PricingCoordinator], ImageEntity):
    """Ecotricity's chart of today's time-of-use prices, for the current season and day type.

    The chart follows the GST option. Ecotricity's GST-inclusive charts also add GST to export
    rows, unlike the integration's export price sensors.
    """

    _attr_content_type = "image/png"

    def __init__(
        self, hass: HomeAssistant, coordinator: PricingCoordinator, icp: str, local_tz: tzinfo
    ) -> None:
        """Initialise the image."""
        EcotricityEntity.__init__(
            self,
            coordinator,
            icp,
            EntityDescription(key="time_of_use_chart", translation_key="time_of_use_chart"),
        )
        ImageEntity.__init__(self, hass)
        self._local_tz = local_tz
        self._attr_image_url = self._chart_url()
        self._attr_image_last_updated = dt_util.utcnow()

    def _chart_url(self) -> str | None:
        data = self.coordinator.data
        today = dt_util.now(self._local_tz)
        template = (
            data.plan.timeframe_chart_weekday
            if today.weekday() < WEEKDAY_COUNT
            else data.plan.timeframe_chart_weekend
        )
        season = data.season
        # Without a published season the chart's dates are unknown, so there's no chart to show.
        if not template or (season is None and needs_season(template)):
            return None
        include_gst = self.coordinator.config_entry.options.get(
            CONF_INCLUDE_GST, DEFAULT_INCLUDE_GST
        )
        path = chart_path(
            template,
            season.start if season else None,
            season.end if season else None,
            include_gst=include_gst,
        )
        # The template comes from the API; a missing leading slash would let it change the host.
        if not path.startswith("/"):
            path = "/" + path
        return ONBOARDING_URL + path

    @callback
    def _handle_coordinator_update(self) -> None:
        """Point at a new chart when the season, day type or plan changes."""
        if (url := self._chart_url()) != self._attr_image_url:
            self._attr_image_url = url
            self._attr_image_last_updated = dt_util.utcnow()
            self._cached_image = None
        super()._handle_coordinator_update()
