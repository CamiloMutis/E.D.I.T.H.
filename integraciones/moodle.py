"""Acceso a Moodle (plataforma académica) para leer los próximos eventos del calendario.

Usa la API oficial de servicios web de Moodle, la misma que usa la app móvil:
se pide un token con usuario y clave y con ese token se consultan las
funciones ``core_calendar_*``. No se lee HTML, así que no se rompe si cambia
el tema visual de la plataforma.

Configuración en ``.env``:
    MOODLE_URL=https://pregradov.usc.edu.co
    MOODLE_USUARIO=...
    MOODLE_CLAVE=...

Uso desde la terminal:
    python -m integraciones.moodle            # eventos de los próximos 30 días
    python -m integraciones.moodle --dias 60
    python -m integraciones.moodle --json
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

SERVICIO = "moodle_mobile_app"


class ErrorMoodle(Exception):
    """Moodle rechazó el acceso o la consulta."""


@dataclass(frozen=True)
class EventoMoodle:
    id: int
    nombre: str
    curso: str
    inicio: datetime
    duracion_min: int
    tipo: str  # due, course, user, site, group, open, close...
    modulo: str  # assign, quiz, forum... o "" si es un evento suelto
    url: str
    descripcion: str = ""

    def como_dict(self) -> dict:
        datos = asdict(self)
        datos["inicio"] = self.inicio.isoformat()
        return datos


class ClienteMoodle:
    def __init__(
        self,
        url: str,
        usuario: str,
        clave: str,
        zona: ZoneInfo | None = None,
        transporte: httpx.BaseTransport | None = None,
    ):
        """``transporte`` permite a las pruebas responder sin red (``httpx.MockTransport``)."""
        self.url = url.rstrip("/")
        self.zona = zona or ZoneInfo("America/Bogota")
        self._http = httpx.Client(timeout=30, follow_redirects=True, transport=transporte)
        self._cache_urls: dict[tuple[str, int], str] = {}
        self._token = self._pedir_token(usuario, clave)

    @classmethod
    def desde_entorno(cls, zona: ZoneInfo | None = None) -> "ClienteMoodle":
        faltan = [v for v in ("MOODLE_URL", "MOODLE_USUARIO", "MOODLE_CLAVE") if not os.getenv(v)]
        if faltan:
            raise ErrorMoodle(f"Faltan variables en .env: {', '.join(faltan)}")
        return cls(os.environ["MOODLE_URL"], os.environ["MOODLE_USUARIO"], os.environ["MOODLE_CLAVE"], zona)

    def _pedir_token(self, usuario: str, clave: str) -> str:
        try:
            respuesta = self._http.post(
                f"{self.url}/login/token.php",
                data={"username": usuario, "password": clave, "service": SERVICIO},
            ).json()
        except (httpx.HTTPError, ValueError) as error:
            raise ErrorMoodle(f"No se pudo conectar con {self.url}: {error}") from error
        if "token" not in respuesta:
            raise ErrorMoodle(f"Moodle rechazó el inicio de sesión: {respuesta.get('error', respuesta)}")
        return respuesta["token"]

    def llamar(self, funcion: str, **parametros) -> dict | list:
        """Llama una función de la API REST de Moodle."""
        datos = {"wstoken": self._token, "wsfunction": funcion, "moodlewsrestformat": "json", **parametros}
        try:
            respuesta = self._http.post(f"{self.url}/webservice/rest/server.php", data=datos).json()
        except (httpx.HTTPError, ValueError) as error:
            raise ErrorMoodle(f"Falló {funcion}: {error}") from error
        if isinstance(respuesta, dict) and "exception" in respuesta:
            raise ErrorMoodle(f"{funcion}: {respuesta.get('message')}")
        return respuesta

    def usuario(self) -> str:
        return self.llamar("core_webservice_get_site_info").get("fullname", "")

    def proximos_eventos(
        self, dias: int = 30, desde: datetime | None = None, hasta: datetime | None = None
    ) -> list[EventoMoodle]:
        """Eventos del calendario entre ``desde`` (por defecto, ahora) y ``hasta`` (por defecto, ``dias`` después)."""
        ahora = desde or datetime.now(self.zona)
        hasta = hasta or ahora + timedelta(days=dias)
        cursos = self._nombres_cursos()
        parametros = {
            "options[userevents]": 1,
            "options[siteevents]": 1,
            "options[timestart]": int(ahora.timestamp()),
            "options[timeend]": int(hasta.timestamp()),
        }
        # Sin lista de cursos, Moodle solo devuelve los eventos personales y del sitio.
        for i, curso_id in enumerate(cursos):
            parametros[f"events[courseids][{i}]"] = curso_id
        respuesta = self.llamar("core_calendar_get_calendar_events", **parametros)
        eventos = [self._convertir(e, cursos) for e in respuesta.get("events", [])]
        return sorted(eventos, key=lambda e: e.inicio)

    def _nombres_cursos(self) -> dict[int, str]:
        try:
            cursos = self.llamar(
                "core_course_get_enrolled_courses_by_timeline_classification", classification="all"
            ).get("courses", [])
        except ErrorMoodle:
            return {}
        return {c["id"]: c["fullname"] for c in cursos}

    def _convertir(self, e: dict, cursos: dict[int, str]) -> EventoMoodle:
        curso_id = e.get("courseid") or 0
        url = self._url_actividad(e.get("modulename"), e.get("instance"))
        return EventoMoodle(
            id=e["id"],
            nombre=e["name"],
            curso=cursos.get(curso_id, ""),
            inicio=datetime.fromtimestamp(e["timestart"], self.zona),
            duracion_min=int(e.get("timeduration", 0)) // 60,
            tipo=e.get("eventtype", ""),
            modulo=e.get("modulename") or "",
            url=url or f"{self.url}/calendar/view.php?view=day&time={e['timestart']}",
            descripcion=_sin_html(e.get("description", "")),
        )

    def _url_actividad(self, modulo: str | None, instancia: int | None) -> str:
        """Enlace a la actividad. El evento trae el id de la instancia, pero la URL usa el del módulo del curso."""
        if not modulo or not instancia:
            return ""
        clave = (modulo, instancia)
        if clave not in self._cache_urls:
            try:
                cm = self.llamar("core_course_get_course_module_by_instance", module=modulo, instance=instancia)
                self._cache_urls[clave] = f"{self.url}/mod/{modulo}/view.php?id={cm['cm']['id']}"
            except (ErrorMoodle, KeyError):
                self._cache_urls[clave] = ""
        return self._cache_urls[clave]

    def cerrar(self) -> None:
        self._http.close()


def _sin_html(texto: str) -> str:
    import html
    import re

    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", texto or "")).split())


def main() -> int:
    from ajustes import cargar_ajustes

    analizador = argparse.ArgumentParser(description="Próximos eventos del calendario de Moodle")
    analizador.add_argument("--dias", type=int, default=30, help="cuántos días hacia adelante (30 por defecto)")
    analizador.add_argument("--json", action="store_true", help="salida en JSON")
    argumentos = analizador.parse_args()

    ajustes = cargar_ajustes()  # también carga .env
    try:
        cliente = ClienteMoodle.desde_entorno(ajustes.zona)
        eventos = cliente.proximos_eventos(argumentos.dias)
    except ErrorMoodle as error:
        print(f"Error: {error}")
        return 1

    if argumentos.json:
        print(json.dumps([e.como_dict() for e in eventos], ensure_ascii=False, indent=2))
        return 0

    print(f"{cliente.usuario()} — {len(eventos)} eventos en los próximos {argumentos.dias} días\n")
    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    for e in eventos:
        print(f"{dias[e.inicio.weekday()]} {e.inicio:%d/%m %H:%M}  {e.nombre}")
        print(f"                {e.curso}  [{e.modulo or e.tipo}]  {e.url}")
    cliente.cerrar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
