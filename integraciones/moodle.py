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
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

SERVICIO = "moodle_mobile_app"


class ErrorMoodle(Exception):
    """Moodle rechazó el acceso o la consulta."""


class ErrorSesionMoodle(ErrorMoodle):
    """El token ya no sirve (cambio de clave, token revocado o vencido): hay que volver a conectar."""


# Códigos con los que Moodle indica que el token no es válido.
CODIGOS_SESION = {"invalidtoken"}


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
    instancia: int = 0  # id de la tarea o del quiz dentro de su módulo
    curso_id: int = 0
    apertura: datetime | None = None  # desde cuándo se puede entregar o presentar
    # Tareas: new | draft | submitted | reopened | sin_envio. Quices: finished | sin_intentos.
    # Vacío si no aplica o si Moodle no respondió.
    estado_entrega: str = ""

    @property
    def entregado(self) -> bool:
        return self.estado_entrega in ESTADOS_ENTREGADO

    def como_dict(self) -> dict:
        datos = asdict(self)
        datos["inicio"] = self.inicio.isoformat()
        datos["apertura"] = self.apertura.isoformat() if self.apertura else None
        datos["entregado"] = self.entregado
        return datos


ESTADOS_ENTREGADO = {"submitted", "finished"}


@dataclass(frozen=True)
class NotificacionMoodle:
    """Aviso de Moodle de que un profesor agregó o cambió contenido en un curso."""

    id: int
    curso: str
    titulo: str
    url: str
    fecha: datetime
    tipo: str  # nuevo | cambio


class ClienteMoodle:
    def __init__(
        self,
        url: str,
        usuario: str,
        clave: str,
        zona: ZoneInfo | None = None,
        transporte: httpx.BaseTransport | None = None,
        token: str | None = None,
    ):
        """Inicia sesión con usuario y clave, o reutiliza ``token`` si se entrega (entonces la clave no se usa).

        ``transporte`` permite a las pruebas responder sin red (``httpx.MockTransport``).
        """
        self.url = url.rstrip("/")
        self.zona = zona or ZoneInfo("America/Bogota")
        self._http = httpx.Client(timeout=30, follow_redirects=True, transport=transporte)
        self._cache_urls: dict[tuple[str, int], str] = {}
        self._cache_nombres_cm: dict[int, str] = {}
        # Lo entregado no vuelve atrás (salvo que el profesor reabra la tarea, caso raro):
        # recordarlo ahorra una consulta por tarea en cada revisión.
        self._entregados: set[tuple[str, int]] = set()
        self._id_usuario: int | None = None
        self._token = token or self._pedir_token(usuario, clave)

    @classmethod
    def con_token(
        cls, url: str, token: str, zona: ZoneInfo | None = None, transporte: httpx.BaseTransport | None = None
    ) -> "ClienteMoodle":
        """Cliente a partir de un token ya obtenido; es lo que usa el bot con cada estudiante."""
        return cls(url, "", "", zona, transporte, token=token)

    @property
    def token(self) -> str:
        return self._token

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
            if respuesta.get("errorcode") in CODIGOS_SESION:
                raise ErrorSesionMoodle(f"{funcion}: {respuesta.get('message')}")
            raise ErrorMoodle(f"{funcion}: {respuesta.get('message')}")
        return respuesta

    def usuario(self) -> str:
        return self.llamar("core_webservice_get_site_info").get("fullname", "")

    def proximos_eventos(
        self,
        dias: int = 30,
        desde: datetime | None = None,
        hasta: datetime | None = None,
        con_entregas: bool = True,
    ) -> list[EventoMoodle]:
        """Eventos del calendario entre ``desde`` (por defecto, ahora) y ``hasta`` (por defecto, ``dias`` después).

        Con ``con_entregas`` se agrega a tareas y quices su fecha de apertura y si ya se entregaron.
        """
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
        if con_entregas:
            eventos = self.completar_entregas(eventos)
        return sorted(eventos, key=lambda e: e.inicio)

    # ------------------------------------------------------- apertura y entregas

    def completar_entregas(self, eventos: list[EventoMoodle]) -> list[EventoMoodle]:
        """Agrega fecha de apertura y estado de entrega a las tareas y los quices.

        Un fallo en una actividad no tumba la consulta: ese evento queda con
        estado vacío (desconocido) y se sigue tratando como pendiente.
        """
        aperturas = {
            "assign": self._aperturas("assign", eventos),
            "quiz": self._aperturas("quiz", eventos),
        }
        salida = []
        for e in eventos:
            if e.modulo not in aperturas or not e.instancia:
                salida.append(e)
                continue
            marca = aperturas[e.modulo].get(e.instancia)
            salida.append(
                replace(
                    e,
                    apertura=datetime.fromtimestamp(marca, self.zona) if marca else None,
                    estado_entrega=self._estado_entrega(e.modulo, e.instancia),
                )
            )
        return salida

    def _aperturas(self, modulo: str, eventos: list[EventoMoodle]) -> dict[int, int]:
        cursos = sorted({e.curso_id for e in eventos if e.modulo == modulo and e.curso_id})
        if not cursos:
            return {}
        parametros = {f"courseids[{i}]": c for i, c in enumerate(cursos)}
        try:
            if modulo == "assign":
                respuesta = self.llamar("mod_assign_get_assignments", **parametros)
                return {
                    a["id"]: int(a.get("allowsubmissionsfromdate") or 0)
                    for curso in respuesta.get("courses", [])
                    for a in curso.get("assignments", [])
                }
            respuesta = self.llamar("mod_quiz_get_quizzes_by_courses", **parametros)
            return {q["id"]: int(q.get("timeopen") or 0) for q in respuesta.get("quizzes", [])}
        except ErrorMoodle:
            return {}

    def _estado_entrega(self, modulo: str, instancia: int) -> str:
        clave = (modulo, instancia)
        if clave in self._entregados:
            return "submitted" if modulo == "assign" else "finished"
        try:
            if modulo == "assign":
                intento = self.llamar("mod_assign_get_submission_status", assignid=instancia).get("lastattempt") or {}
                # En tareas grupales cuenta el envío del grupo.
                estados = [
                    envio.get("status")
                    for envio in (intento.get("submission"), intento.get("teamsubmission"))
                    if envio and envio.get("status")
                ]
                estado = "submitted" if "submitted" in estados else (estados[0] if estados else "sin_envio")
            else:
                intentos = self.llamar("mod_quiz_get_user_attempts", quizid=instancia, status="finished")
                estado = "finished" if intentos.get("attempts") else "sin_intentos"
        except ErrorMoodle:
            return ""
        if estado in ESTADOS_ENTREGADO:
            self._entregados.add(clave)
        return estado

    # ---------------------------------------------------------- contenido nuevo

    def notificaciones_contenido(self, despues_de: int = 0, limite: int = 50) -> list[NotificacionMoodle]:
        """Avisos de "contenido nuevo" o "cambio de contenido" con id mayor a ``despues_de``, del más viejo al más nuevo.

        Moodle los genera cuando el profesor marca "notificar cambio de contenido"
        al crear o editar una actividad (diapositivas, lecturas, tareas...).
        """
        if self._id_usuario is None:
            id_usuario = self.llamar("core_webservice_get_site_info").get("userid")
            if not id_usuario:
                raise ErrorMoodle("Moodle no informó el id del usuario")
            self._id_usuario = int(id_usuario)
        respuesta = self.llamar("message_popup_get_popup_notifications", useridto=self._id_usuario, limit=limite)
        salida = []
        for n in respuesta.get("notifications", []):
            if n.get("eventtype") != "coursecontentupdated" or n["id"] <= despues_de:
                continue
            # Asunto: "2026B/LENGUAJES DE PROGRAMACION/LEN03 contenido nuevo" (o "cambio de contenido").
            asunto = n.get("subject", "").strip()
            url = n.get("contexturl") or ""
            salida.append(
                NotificacionMoodle(
                    id=n["id"],
                    curso=re.sub(r"\s+(contenido nuevo|cambio de contenido)$", "", asunto),
                    titulo=self._nombre_actividad(url),
                    url=url,
                    fecha=datetime.fromtimestamp(n["timecreated"], self.zona),
                    tipo="cambio" if asunto.endswith("cambio de contenido") else "nuevo",
                )
            )
        return sorted(salida, key=lambda n: n.id)

    def _nombre_actividad(self, url: str) -> str:
        """Nombre de la actividad a partir de su URL (``.../view.php?id=<cmid>``); vacío si no se puede."""
        coincidencia = re.search(r"[?&]id=(\d+)", url)
        if not coincidencia:
            return ""
        cmid = int(coincidencia.group(1))
        if cmid not in self._cache_nombres_cm:
            try:
                self._cache_nombres_cm[cmid] = self.llamar("core_course_get_course_module", cmid=cmid)["cm"]["name"]
            except (ErrorMoodle, KeyError):
                self._cache_nombres_cm[cmid] = ""
        return self._cache_nombres_cm[cmid]

    def _nombres_cursos(self) -> dict[int, str]:
        # Si falla, el error sube: devolver {} haría creer que no hay eventos de ningún curso.
        cursos = self.llamar(
            "core_course_get_enrolled_courses_by_timeline_classification", classification="all"
        ).get("courses", [])
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
            instancia=int(e.get("instance") or 0),
            curso_id=int(curso_id),
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
        marca = "✓ entregado" if e.entregado else ("borrador sin enviar" if e.estado_entrega == "draft" else "")
        abre = f"abre {e.apertura:%d/%m %H:%M}" if e.apertura and e.apertura > datetime.now(e.apertura.tzinfo) else ""
        extra = "  ".join(x for x in (marca, abre) if x)
        print(f"{dias[e.inicio.weekday()]} {e.inicio:%d/%m %H:%M}  {e.nombre}" + (f"   [{extra}]" if extra else ""))
        print(f"                {e.curso}  [{e.modulo or e.tipo}]  {e.url}")
    cliente.cerrar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
