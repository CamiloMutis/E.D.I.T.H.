"""Calendario respaldado por Google Calendar API.

La primera ejecución abre el navegador para autorizar el acceso y guarda el
token en ``token.json``; las siguientes lo reutilizan y lo renuevan solas.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from integraciones.calendario import Evento

registro = logging.getLogger(__name__)

PERMISOS = ["https://www.googleapis.com/auth/calendar.events"]


class GoogleCalendar:
    """Lee los eventos del usuario y crea, mueve o borra los bloques de estudio."""

    nombre = "google"

    def __init__(
        self,
        zona: ZoneInfo,
        ruta_credenciales: Path,
        ruta_token: Path,
        calendario_id: str = "primary",
    ):
        # Las dependencias de Google se importan aquí para que el resto del
        # proyecto funcione aunque se use solo el calendario local.
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        self.zona = zona
        self.calendario_id = calendario_id

        credenciales = None
        if ruta_token.exists():
            credenciales = Credentials.from_authorized_user_file(str(ruta_token), PERMISOS)
        if not credenciales or not credenciales.valid:
            if credenciales and credenciales.expired and credenciales.refresh_token:
                credenciales.refresh(Request())
            else:
                flujo = InstalledAppFlow.from_client_secrets_file(str(ruta_credenciales), PERMISOS)
                credenciales = flujo.run_local_server(port=0)
            ruta_token.write_text(credenciales.to_json(), encoding="utf-8")
        self._servicio = build("calendar", "v3", credentials=credenciales, cache_discovery=False)

    def listar_eventos(self, desde: datetime, hasta: datetime) -> list[Evento]:
        eventos: list[Evento] = []
        pagina = None
        while True:
            respuesta = (
                self._servicio.events()
                .list(
                    calendarId=self.calendario_id,
                    timeMin=desde.isoformat(),
                    timeMax=hasta.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=pagina,
                )
                .execute()
            )
            for item in respuesta.get("items", []):
                # Los eventos de día completo (cumpleaños, festivos) no traen
                # hora y no bloquean tiempo de estudio.
                if "dateTime" not in item.get("start", {}) or item.get("transparency") == "transparent":
                    continue
                eventos.append(
                    Evento(
                        id=item["id"],
                        titulo=item.get("summary", "(sin título)"),
                        inicio=datetime.fromisoformat(item["start"]["dateTime"]).astimezone(self.zona),
                        fin=datetime.fromisoformat(item["end"]["dateTime"]).astimezone(self.zona),
                    )
                )
            pagina = respuesta.get("nextPageToken")
            if not pagina:
                return eventos

    def _cuerpo_fechas(self, inicio: datetime, fin: datetime) -> dict:
        return {
            "start": {"dateTime": inicio.isoformat(), "timeZone": str(self.zona)},
            "end": {"dateTime": fin.isoformat(), "timeZone": str(self.zona)},
        }

    def crear_evento(self, titulo: str, inicio: datetime, fin: datetime, descripcion: str = "") -> str:
        cuerpo = {
            "summary": titulo,
            "description": descripcion,
            "extendedProperties": {"private": {"edith": "1"}},
            **self._cuerpo_fechas(inicio, fin),
        }
        creado = self._servicio.events().insert(calendarId=self.calendario_id, body=cuerpo).execute()
        return creado["id"]

    def mover_evento(self, evento_id: str, inicio: datetime, fin: datetime) -> None:
        self._servicio.events().patch(
            calendarId=self.calendario_id, eventId=evento_id, body=self._cuerpo_fechas(inicio, fin)
        ).execute()

    def eliminar_evento(self, evento_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._servicio.events().delete(calendarId=self.calendario_id, eventId=evento_id).execute()
        except HttpError as error:
            # Si el usuario ya lo borró a mano no hay nada que hacer.
            if error.resp.status not in (404, 410):
                raise
            registro.info("El evento %s ya no existía en Google Calendar", evento_id)


def crear_calendario(ajustes, repo):
    """Elige el calendario según ``config.yaml`` y las credenciales disponibles."""
    from ajustes import RAIZ
    from integraciones.calendario import CalendarioLocal

    def _ruta(variable: str, defecto: str) -> Path:
        ruta = Path(os.getenv(variable) or defecto)
        return ruta if ruta.is_absolute() else RAIZ / ruta

    credenciales = _ruta("GOOGLE_CREDENTIALS", "credentials.json")
    backend = ajustes.calendario_backend
    if backend == "google" or (backend == "auto" and credenciales.exists()):
        if not credenciales.exists():
            raise FileNotFoundError(
                f"calendario.backend es 'google' pero no existe {credenciales}. "
                "Descarga las credenciales OAuth o cambia el backend a 'local'."
            )
        return GoogleCalendar(
            ajustes.zona,
            credenciales,
            _ruta("GOOGLE_TOKEN", "token.json"),
            os.getenv("GOOGLE_CALENDAR_ID") or "primary",
        )
    return CalendarioLocal(repo, ajustes.zona)
