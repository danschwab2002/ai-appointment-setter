"""La campana por la landing que vio la persona (E-321).

Contrato: ``docs/contracts/campaign-entry-v1.md``. Diseno: E-319, adenda del
03/11 (decision de Dan: el carrito de una campana se identifica por la landing).

Las landings de una campana (Black November de ATT1) son otras que las de
siempre. Cuando una persona deja el formulario en una de ellas, la intencion
guarda su ``landing_ref``; cuando despues llega su carrito, la admision portable
lo correlaciona con esa intencion, y el recorrido nativo del carrito nace con
ella (``flow_engine_native_runs.audience_purchase_intent_id``). Este modulo hace
que, si esa landing esta declarada en una campana vigente (``[[campanas]]`` del
manifiesto), el motor le ponga a la conversacion la etiqueta del segmento de la
campana **antes del primer envio**, con el mismo mecanismo de E-316
(``NativeSegmentLabelPlan``: el macro que solo suma la etiqueta, antes de la
plantilla, y un reintento despues del envio aceptado). Cuando la persona
contesta, el webhook ya trae la etiqueta y el agente responde con el
conocimiento de la campana.

La oferta propia de la campana es el respaldo: si la campana declara
``ofertas`` y el evento es de una de ellas, tambien la identifica (sirve para el
carrito sin formulario). La landing manda sobre la oferta.

Lo que no hace:

* Sin el flag (``FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED``) no se arma nada y el
  despacho no lee nada nuevo.
* Fuera de la vigencia de la campana (la del archivo del segmento, PR-A) no pone
  nada. Sin corte por horario.
* La compra no entra (``cliente`` sigue por su camino): solo los recorridos que
  nacen de una intencion (carrito, pago rechazado, primer contacto).
* Si el paso ``etiquetar`` del flujo ya nombra un segmento (E-316), manda el del
  flujo: una sola etiqueta por camino (D-E316-2).

Los logs no llevan telefono, mail ni nombre: la accion, la campana, la landing
o la oferta y el disparador.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .segment_labels import (
    STATE_ACTIVE,
    NativeSegmentLabelPlan,
    SegmentLabel,
    SegmentLabelsConfigError,
    declared_segment_labels,
)

logger = logging.getLogger(__name__)

# Los disparadores nativos que nacen de una intencion (la fila del recorrido
# lleva la intencion que dio el permiso). La compra no: la persona ya es
# cliente y su etiqueta es otra.
CAMPAIGN_TRIGGERS = frozenset({"carrito", "pago_fallido", "intencion"})
MATCH_LANDING = "landing"
MATCH_OFFER = "oferta"


class _Labeler(Protocol):
    @property
    def label(self) -> str: ...

    async def ensure(self, conversation_id: int, *, source: str) -> str: ...


class _Planner(Protocol):
    async def plan(self, supabase: Any, *, action_id: str) -> NativeSegmentLabelPlan | None: ...


@dataclass(frozen=True)
class CampaignEntry:
    """Una campana vista desde lo que la identifica: su segmento, sus landings y sus ofertas."""

    segment: SegmentLabel
    landings: frozenset[str]
    offers: frozenset[str]


def declared_campaigns(manifest: object, catalog: object) -> tuple[CampaignEntry, ...]:
    """Las campanas del manifiesto, cada una con el segmento de PR-A y su vigencia.

    ``manifest.campaigns`` (``[[campanas]]``) nombra segmentos por su clave; el
    manifiesto ya valido que exista y que tenga macro. La vigencia sale del
    catalogo de PR-A (``declared_segment_labels``).
    """
    by_key = {segment.key: segment for segment in declared_segment_labels(manifest, catalog)}
    out: list[CampaignEntry] = []
    for campaign in getattr(manifest, "campaigns", ()) or ():
        segment = by_key.get(campaign.segment)
        if segment is None or segment.macro_id is None:
            raise SegmentLabelsConfigError("campaign_segment_invalid")
        out.append(
            CampaignEntry(
                segment=segment,
                landings=frozenset(campaign.landings),
                offers=frozenset(campaign.offers),
            )
        )
    return tuple(out)


def match_campaign(
    campaigns: Iterable[CampaignEntry],
    *,
    landing: str | None,
    offer: str | None,
    now: datetime,
) -> tuple[CampaignEntry, str] | None:
    """La campana vigente de esta landing u oferta, y por cual de las dos.

    La landing manda: si la landing es de una campana vigente, esa. Si no, la
    oferta. Una campana por empezar, vencida o sin vigencia conocida no cuenta.
    """
    active = [campaign for campaign in campaigns if campaign.segment.state_at(now) == STATE_ACTIVE]
    if landing is not None:
        for campaign in active:
            if landing in campaign.landings:
                return campaign, MATCH_LANDING
    if offer is not None:
        for campaign in active:
            if offer in campaign.offers:
                return campaign, MATCH_OFFER
    return None


class _OriginReader(Protocol):
    async def get_flow_engine_native_campaign_origin(self, action_id: str) -> Any: ...


class CampaignEntrySource(Protocol):
    """De donde lee la entrada por landing u oferta sus campanas (E-411).

    Se lee en cada ``plan``. La fija es la de hoy (``[[campanas]]``); la viva
    suma las campanas del Automation Expert que sirve el catalogo de la base.
    """

    live: bool

    def campaigns(self) -> tuple[CampaignEntry, ...]: ...

    def labeler(self, label: str) -> _Labeler | None: ...


class StaticCampaignEntries:
    """La fuente fija: las ``[[campanas]]`` del manifiesto y sus etiquetadores."""

    live = False

    def __init__(self, campaigns: Iterable[CampaignEntry], labelers: Mapping[str, _Labeler]) -> None:
        self._campaigns = tuple(campaigns)
        self._labelers = dict(labelers)

    def campaigns(self) -> tuple[CampaignEntry, ...]:
        return self._campaigns

    def labeler(self, label: str) -> _Labeler | None:
        return self._labelers.get(label)


def agent_campaign_entries(
    served: Iterable[tuple[SegmentLabel, str | None]], offers: Iterable[Any]
) -> tuple[CampaignEntry, ...]:
    """Las campanas del Automation Expert que entran por su oferta (E-411, D4).

    ``served``: cada campana servida, con su ``oferta.codigo_oferta`` (E-410).
    ``offers``: las ofertas declaradas de la instancia (``code``, ``landing_id``),
    de todos sus productos. La campana entra por esa oferta y por la landing de
    esa oferta en el manifiesto (cada landing es de una sola oferta). Sin
    ``codigo_oferta``, o con una oferta que la instancia no declara, no entra.
    """
    by_code = {getattr(offer, "code", None): offer for offer in offers}
    out: list[CampaignEntry] = []
    for segment, code in served:
        if code is None or segment.macro_id is None:
            continue
        offer = by_code.get(code)
        if offer is None:
            continue
        landing = getattr(offer, "landing_id", None)
        out.append(
            CampaignEntry(
                segment=segment,
                landings=frozenset({landing}) if isinstance(landing, str) and landing else frozenset(),
                offers=frozenset({code}),
            )
        )
    return tuple(out)


class CampaignEntryLabels:
    """El plan de la etiqueta de un envio nativo: el del flujo (E-316) o el de la campana.

    Tiene la misma forma que ``NativeSegmentLabels`` (``plan``), asi el
    despachador no cambia: va en su lugar.
    """

    def __init__(
        self,
        *,
        campaigns: Iterable[CampaignEntry] = (),
        labelers: Mapping[str, _Labeler] | None = None,
        source: CampaignEntrySource | None = None,
        flow_labels: _Planner | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if source is None:
            # La fuente fija de hoy: se valida al construir, como siempre.
            campaigns = tuple(campaigns)
            labelers = dict(labelers or {})
            if not campaigns:
                raise SegmentLabelsConfigError("campaign_entry_invalid")
            for campaign in campaigns:
                labeler = labelers.get(campaign.segment.label)
                if labeler is None or labeler.label != campaign.segment.label:
                    raise SegmentLabelsConfigError("segment_labelers_invalid")
            source = StaticCampaignEntries(campaigns, labelers)
        self._source = source
        self._flow_labels = flow_labels
        self._clock = clock

    async def plan(self, supabase: Any, *, action_id: str) -> NativeSegmentLabelPlan | None:
        """El plan del flujo si lo hay; si no, el de la campana de la landing u oferta. Nunca lanza."""
        if self._flow_labels is not None:
            flow_plan = await self._flow_labels.plan(supabase, action_id=action_id)
            if flow_plan is not None:
                return flow_plan
        campaigns = self._source.campaigns()
        if not campaigns:
            # E-411: sin campanas (la fuente viva vacia) no se lee el origen.
            return None
        reader: _OriginReader = supabase
        try:
            origin = await reader.get_flow_engine_native_campaign_origin(action_id)
        except Exception as exc:  # noqa: BLE001 - falla blanda, el log lleva el tipo
            logger.warning(
                "campaign_entry_origin_unavailable action_id=%s error=%s",
                action_id,
                type(exc).__name__,
            )
            return None
        if origin is None:
            return None
        trigger = getattr(origin, "disparador", None)
        if trigger not in CAMPAIGN_TRIGGERS:
            return None
        landing = getattr(origin, "landing", None)
        offer = getattr(origin, "oferta", None)
        found = match_campaign(campaigns, landing=landing, offer=offer, now=self._clock())
        if found is None:
            return None
        campaign, matched_by = found
        labeler = self._source.labeler(campaign.segment.label)
        if labeler is None:
            return None
        logger.info(
            "campaign_entry_matched action_id=%s segment=%s label=%s by=%s value=%s trigger=%s",
            action_id,
            campaign.segment.key,
            campaign.segment.label,
            matched_by,
            landing if matched_by == MATCH_LANDING else offer,
            trigger,
        )
        return NativeSegmentLabelPlan(
            action_id=action_id,
            segment=campaign.segment,
            labeler=labeler,
        )


def campaign_entry_labels(
    settings: Any, control_client: Any, flow_labels: _Planner | None, *, live: Any = None
) -> _Planner | None:
    """El planificador del despacho nativo con las campanas por landing, o ``flow_labels`` sin el flag.

    ``FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED`` pide el motor de flujos, los segmentos
    del agente cargados (``AGENT_SEGMENTS_ENABLED``: la vigencia de la campana
    sale de ahi), el cliente de control de Chatwoot y al menos una
    ``[[campanas]]``. Falla cerrado. El macro de una campana no puede ser el de
    la pausa, la reanudacion, la baja ni el de cliente.

    ``live`` (E-411, con ``AGENT_CAMPAIGN_LABEL_PATHS_ENABLED``): el catalogo vivo.
    Con el, el arranque no exige ``[[campanas]]`` y el plan lee las campanas del
    manifiesto mas las del AE con ``codigo_oferta``. Sin ``live``, lo de hoy.
    """
    from .product_label import ProductLabeler

    if not getattr(settings, "flow_engine_campaign_entry_enabled", False):
        return flow_labels
    flag = "FLOW_ENGINE_CAMPAIGN_ENTRY_ENABLED"
    manifest = getattr(settings, "instance_manifest", None)
    if manifest is None:
        raise ValueError(f"{flag} requires an instance manifest")
    if not getattr(settings, "flow_engine_enabled", False):
        raise ValueError(f"{flag} requires FLOW_ENGINE_ENABLED")
    catalog = getattr(settings, "agent_segment_catalog", None)
    if catalog is None or not getattr(settings, "agent_segments_enabled", False):
        raise ValueError(f"{flag} requires AGENT_SEGMENTS_ENABLED")
    if control_client is None:
        raise ValueError(f"{flag} requires the Chatwoot control client")
    campaigns = declared_campaigns(manifest, catalog)
    if not campaigns and live is None:
        raise ValueError(f"{flag} requires [[campanas]] in the manifest")
    reserved = {
        getattr(settings, name, None)
        for name in (
            "chatwoot_pause_macro_id",
            "chatwoot_resume_macro_id",
            "chatwoot_opt_out_macro_id",
            "chatwoot_client_label_macro_id",
        )
    } - {None}
    labelers: dict[str, ProductLabeler] = {}
    for campaign in campaigns:
        segment = campaign.segment
        if segment.macro_id in reserved:
            raise ValueError(
                "a campaign label macro cannot be a pause, resume, opt-out or client macro"
            )
        assert segment.macro_id is not None
        labelers[segment.label] = ProductLabeler(
            client=control_client, label=segment.label, macro_id=segment.macro_id
        )
    if live is not None:
        live.bind_entry_campaigns(
            campaigns,
            labelers=labelers,
            make_labeler=lambda label, macro_id: ProductLabeler(
                client=control_client, label=label, macro_id=macro_id
            ),
        )
        return CampaignEntryLabels(source=live.entry_source(), flow_labels=flow_labels)
    return CampaignEntryLabels(campaigns=campaigns, labelers=labelers, flow_labels=flow_labels)
