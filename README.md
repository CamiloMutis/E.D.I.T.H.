# E.D.I.T.H.
### Estudiante Dirigido por Inteligencia, Tareas y Horarios

> *"Even Dead, I'm The Hero."*

En *Spider-Man: Lejos de casa*, Tony Stark le deja a Peter Parker unas gafas con
**E.D.I.T.H.**, una inteligencia artificial capaz de ver todo, anticiparse y actuar
por él. Peter tenía que salvar el mundo; nosotros solo tenemos que sobrevivir al
semestre. Pero el problema se parece más de lo que crees: demasiadas cosas pasando
al mismo tiempo y muy poco control sobre ellas.

Este proyecto nace en la materia de **Inteligencia Artificial**, cuyo reto era
resolver una problemática real mediante un **agente inteligente con herramientas
y skills**. Elegimos la que vivimos todos los días: perder el control
del
cronograma académico. Entregas que llegan sin avisar, planes que se
rompen el
primer día, informes "de 2 horas" que terminan tomando 5, y archivos
perdidos
entre Descargas, Drive y el correo.

**E.D.I.T.H. es nuestra respuesta:** un agente que no solo te recuerda
las cosas,
sino que dirige tu semestre como un orquestador:

- 📅 **Planea** cada entrega hacia atrás y reserva bloques de trabajo en tu Google Calendar.
- 🧠 **Predice** cuánto te vas a demorar *de verdad*, con un modelo de ML entrenado sobre tu historial.
- 📁 **Organiza** tus archivos por materia con un clasificador entrenado.
- 🔁 **Corrige el rumbo** cada noche: si te atrasaste, reacomoda tu semana y te avisa qué entregas están en riesgo.

## Cómo trabaja

Se le habla por Telegram y actúa sobre el calendario, la carpeta de archivos y una base de datos propia. Un mismo bot atiende a varios estudiantes: cada uno se registra desde el chat, conecta su Moodle y tiene su propia base de datos, sus archivos y su memoria de conversación. Cada frente tiene su skill:

| Frente | Skill | Qué hace |
|---|---|---|
| Planear | `planificar_entrega` | Divide una entrega en subtareas y las agenda en los huecos libres del calendario, de la fecha límite hacia atrás |
| Predecir | `estimar_tiempo` | Usa un modelo entrenado para estimar cuántas horas reales tomará cada subtarea |
| Organizar | `organizar_archivos` | Clasifica cada archivo nuevo por materia con un modelo entrenado, lo mueve y lo vincula a su entrega |
| Corregir el rumbo | `replanificar` | Detecta lo que no se hizo, reacomoda la semana y avisa qué entregas quedan en riesgo |

Una conversación típica:

> **Tú:** El viernes 27 entrego el taller 3 de Sistemas Operativos, vale 15 %, es sobre semáforos. Creo que son unas 4 horas.
>
> **E.D.I.T.H.:** Te reservé 5 bloques entre el miércoles y el jueves. Estimaste 4 h, pero para este tipo de trabajo lo esperado son 7,5 h: las tareas de código suelen tomar un 80 % más de lo previsto.

El sistema se retroalimenta: lo que realmente tardas vuelve al historial y el modelo de tiempos se reentrena cada semana.

## Arquitectura

```mermaid
flowchart TD
    U[Usuario en Telegram] <--> A[Agente orquestador<br/>LLM con tool calling]
    P[Programador de tareas<br/>7:00, 22:00, domingo] --> A
    V[Vigilante de archivos<br/>carpeta de descargas] --> A
    A --> S1[planificar_entrega]
    A --> S2[replanificar]
    A --> S3[estimar_tiempo]
    A --> S4[organizar_archivos]
    S1 --> PL[Planificador voraz<br/>determinista]
    S2 --> PL
    S1 -. llama .-> S3
    PL --> C[Calendario<br/>Google Calendar o local]
    S3 --> M1[Predictor de horas<br/>regresión]
    S4 --> M2[Clasificador de materias<br/>TF-IDF]
    S4 --> F[Universidad/Materia/Entrega]
    S1 & S2 & S3 & S4 --> DB[(SQLite)]
    DB -. historial y etiquetas .-> M1 & M2
```

Cada pieza hace lo que mejor sabe hacer:

| Tarea | Responsable | Razón |
|---|---|---|
| Entender lo que escribe el usuario y elegir el skill | LLM | Lenguaje natural y razonamiento |
| Dividir una entrega en subtareas | LLM | Requiere entender el enunciado |
| Estimar horas | Modelo de regresión | Aprende del historial y es medible |
| Clasificar archivos | Modelo de clasificación (LLM si hay duda) | Rápido y sin costo |
| Asignar bloques y calcular el riesgo | Algoritmo determinista | Debe respetar las restricciones siempre |
| Redactar avisos y proponer opciones | LLM | Comunicación y criterio |

## Modelos

Los dos modelos se entrenan con scikit-learn. Cada uno tiene su notebook con el pipeline completo: datos, preprocesamiento, entrenamiento, validación y métricas.

| Modelo | Algoritmo elegido | Resultado en el conjunto de prueba |
|---|---|---|
| Predictor de tiempo (`notebooks/02`) | Gradient Boosting | MAE de 1,09 h frente a 2,36 h de la línea base; R² de 0,81 |
| Clasificador de archivos (`notebooks/03`) | Naive Bayes sobre TF-IDF | Accuracy de 0,994 y F1 macro de 0,994 en 44 materias |

**Sobre los datos.** Estos resultados se obtuvieron con datos sintéticos: tiempos generados con reglas explícitas (`ml/datos_sinteticos.py`) y documentos de práctica armados con el vocabulario de cada materia (`ml/corpus_sintetico.py`). Sirven para dejar funcionando el pipeline y el agente, pero no miden el desempeño con datos reales, que será menor. El proyecto está preparado para reemplazarlos:

- **Tiempos:** la encuesta descrita en [`docs/encuesta.md`](docs/encuesta.md) se guarda en `data/encuesta_tiempos.csv` y entra sola al entrenamiento.
- **Archivos:** cada vez que el clasificador duda y el usuario confirma la materia, ese archivo queda como ejemplo etiquetado para el siguiente reentreno.
- **Uso propio:** cada subtarea terminada agrega una fila real al historial.

## Instalación

Requisitos: Python 3.12 o superior, Git y una cuenta de Telegram. Funciona en Linux, Windows y macOS. Son cuatro comandos; el último te guía en lo demás.

```bash
git clone https://github.com/CamiloMutis/E.D.I.T.H..git edith
cd edith
```

> **Importante: todos los comandos de este README se ejecutan desde la carpeta del proyecto**, es decir, la carpeta `edith` que creó `git clone` (la que tiene `main.py`, `configurar.py` y `servicio.py`). Desde otra carpeta, o desde dentro de `.venv`, fallan con *"can't open file"*. Si abres una terminal nueva, entra primero a ella con `cd ruta/a/edith`; para saber dónde estás, `pwd` (Linux o macOS) o `cd` (Windows).

Linux o macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python configurar.py
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py configurar.py
```

Si PowerShell bloquea la activación, ejecuta antes `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Configuración

Cada persona monta **su propio E.D.I.T.H.**, con su propio bot de Telegram (llámalo como quieras: «EdithBot», «Moodle Bot»…) y sus propias llaves. No hace falta editar nada a mano: `python configurar.py` (o `python main.py` la primera vez) pregunta paso a paso y escribe el `.env`, que nunca se sube al repositorio:

1. **Tu bot.** Lo creas en [@BotFather](https://t.me/BotFather) con `/newbot` y pegas el token; el asistente lo comprueba con Telegram y le pone el nombre visible que quieras.
2. **Tú eres el dueño.** Le escribes «hola» a tu bot y el asistente detecta tu id de Telegram.
3. **El banco de LLM.** Eliges los proveedores en orden (Groq gratis basta), pegas cada llave y se comprueba en el momento.
4. **Quién lo usa.** Por defecto **es privado**: cualquiera puede encontrar un bot buscando su @, pero solo tú lo usas; a los demás les responde «🔒 Este es un asistente privado» sin guardar nada ni gastar tus llaves. Puedes autorizar a algunas personas por su id, usar un código de invitación o abrirlo a todos (`EDITH_ACCESO` y `EDITH_USUARIOS_PERMITIDOS`).
5. **Moodle.** Confirmas la dirección de tu Moodle y se genera la clave de cifrado. Tu usuario y contraseña de Moodle no van en el `.env`: los das en el chat con `/start`.

Si prefieres hacerlo a mano, copia `.env.example` como `.env`; cada variable está explicada ahí. Detalles:

1. **Telegram.** Habla con [@BotFather](https://t.me/BotFather), envía `/newbot` y copia el token en `TELEGRAM_BOT_TOKEN`.
2. **LLM (banco de proveedores).** Crea una llave gratuita en [console.groq.com](https://console.groq.com) y cópiala en `GROQ_API_KEY`; con eso basta. Para que E.D.I.T.H. no se quede sin respuesta cuando se agota la cuota, arma tu propio banco: pon las llaves que tengas (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`…) y el orden en `LLM_PROVIDERS`, por ejemplo `LLM_PROVIDERS=groq,openai,anthropic`. Cuando uno se agota, falla o no responde, la misma conversación sigue con el siguiente sin que el estudiante lo note, y vuelve al primero a los 5 minutos. También puedes poner varias llaves de un proveedor separadas por comas (`GROQ_API_KEY=gsk_1,gsk_2`) y sumar cualquier API compatible con OpenAI, como OpenRouter u Ollama, con `<NOMBRE>_BASE_URL`, `<NOMBRE>_MODEL` y `<NOMBRE>_API_KEY` (ver `.env.example`).
3. **Administrador.** Pon tu id de Telegram (te lo da [@userinfobot](https://t.me/userinfobot)) en `TELEGRAM_ADMIN_ID`: tendrás `/admin`, `/anuncio` y `/reentrenar` y te llegarán los avisos de errores.
4. **Google Calendar (opcional, solo modo consola).** En [Google Cloud Console](https://console.cloud.google.com) crea un proyecto, habilita Google Calendar API, crea una credencial OAuth de tipo "Aplicación de escritorio" y guarda el archivo como `credentials.json` en la raíz. La primera ejecución abre el navegador para autorizar. Sin este archivo, E.D.I.T.H. usa un calendario local guardado en SQLite.
5. **Reglas.** En `config.yaml` se ajustan el semestre, las horas máximas de estudio por día, la franja horaria, el margen antes de cada entrega y las carpetas. En Windows y en Linux la carpeta de descargas por defecto es `~/Downloads`.

Las materias salen del pénsum de Ingeniería de Sistemas (`data/pensum.json`). Al arrancar quedan activas las del semestre indicado en `config.yaml`; se cambia en cualquier momento con `/semestre N`.

## Uso

### Encenderlo

Al terminar, `configurar.py` pregunta cómo quieres usarlo. Para cambiarlo después, abre una terminal **en la carpeta donde clonaste el proyecto** (la que tiene `servicio.py` y `main.py`; no funciona desde otra carpeta) y usa el Python del entorno virtual, sin entrar a la carpeta `.venv`:

```bash
cd ruta/a/edith          # la carpeta que creó git clone
```


```bash
# Linux o macOS
.venv/bin/python servicio.py activar
```

```powershell
# Windows (PowerShell)
.venv\Scripts\python servicio.py activar
```

Si ya activaste el entorno (`source .venv/bin/activate`, `source .venv/bin/activate.fish` en fish o `.venv\Scripts\Activate.ps1` en Windows), basta con `python`, como en la tabla:

| Quiero… | Comando |
|---|---|
| Que esté **siempre encendido** (arranca solo con el computador y se reinicia si falla) | `python servicio.py activar` |
| **Apagarlo** y quitar el arranque automático | `python servicio.py desactivar` |
| Saber si está corriendo | `python servicio.py estado` |
| Ver lo que está haciendo | `python servicio.py registro` |
| Encenderlo solo mientras tenga la terminal abierta | `python main.py` (Ctrl+C lo apaga) |

`activar` se ejecuta **una sola vez**: desde ahí E.D.I.T.H. arranca solo cada vez que prendes el computador, hasta que lo desactives. El servicio guarda la ruta del proyecto, así que no muevas ni renombres la carpeta mientras esté activo (si lo haces, `desactivar` y vuelve a `activar` desde la nueva ubicación).

En Linux usa un servicio de usuario de systemd; en Windows, una tarea programada al iniciar sesión, sin ventana; en macOS, launchd. Ninguno pide permisos de administrador. En Linux, `python servicio.py activar --siempre` lo mantiene encendido aunque no hayas iniciado sesión.

E.D.I.T.H. solo responde mientras está encendido. Telegram deja conectada una sola copia del bot por token: si el servicio está activo, `python main.py` lo avisa en lugar de abrir una segunda.

### Registrarte en tu bot

Escríbele `/start` a tu bot y E.D.I.T.H. te guía en un minuto:

1. Le explica qué datos guarda y pide que acepte (proyecto académico, Ley 1581 de 2012).
2. Le pregunta el semestre.
3. Le pide su usuario y su contraseña de Moodle. **La contraseña se usa una sola vez** para pedirle a Moodle un permiso de acceso (token); el mensaje con la contraseña se borra del chat y solo queda el token, cifrado con `EDITH_CLAVE_MAESTRA`. Se puede saltar y conectar después con `/conectar_moodle`.

Desde ahí se le habla en lenguaje natural o con comandos:

| Comando | Qué hace |
|---|---|
| `/hoy` | Plan del día: bloques, entregas de la semana y archivos relacionados |
| `/pendientes` | Entregas abiertas con su nivel de riesgo |
| `/revisar_dia` | Pregunta cómo te fue con los bloques de hoy y reorganiza la semana |
| `/moodle` | Revisa Moodle ahora: tareas nuevas, fechas cambiadas y lo que está por vencer |
| `/calendario` | Envía tu plan (bloques, entregas y vencimientos de Moodle) como archivo `.ics` para Google Calendar u Outlook |
| `/semestre N` · `/materias` | Cambia el semestre y, con él, las materias activas |
| `/pausar` | Pausa resúmenes, revisiones y avisos de Moodle: `/pausar`, `/pausar 3h`, `/pausar 2d`, `/pausar 1s`, `/pausar hasta 30/11` |
| `/reanudar` | Vuelve a enviar avisos y revisa Moodle de inmediato |
| `/ajustes` | Horas de estudio por día, hora del resumen y de la revisión, horario de avisos de Moodle |
| `/estado` | Qué está conectado, si estás en pausa y cuántos mensajes llevas hoy |
| `/conectar_moodle` · `/desconectar_moodle` | Conecta, reconecta o borra el acceso a Moodle |
| `/mis_datos` | Descarga en un `.zip` todo lo que E.D.I.T.H. guarda de ti |
| `/olvidarme` | Borra tu cuenta, tus datos y tus archivos |

Sin que se lo pidas, E.D.I.T.H. envía a cada estudiante el resumen de la mañana y la revisión de la noche a la hora que eligió (por defecto 7:00 a. m. y 10:00 p. m.). Si el bot estuvo apagado a esa hora, lo envía al volver, hasta dos horas tarde. Los modelos se reentrenan cada domingo con lo que aportan todos los estudiantes. También organiza cualquier PDF, Word o PowerPoint que le envíes por el chat, en la carpeta de cada estudiante.

**Quién puede usarlo.** Tu bot es **privado** por defecto (`EDITH_ACCESO=privado`): solo tú (`TELEGRAM_ADMIN_ID`) y los ids de `EDITH_USUARIOS_PERMITIDOS`. Si alguien más encuentra tu @ y le escribe, recibe una sola vez «🔒 Este es un asistente privado» y nada más: no se le crea cuenta ni se gastan tus llaves. Para compartirlo, `EDITH_ACCESO=invitacion` (con `EDITH_CODIGO_INVITACION`) o `EDITH_ACCESO=publico`. Cada persona tiene un tope de mensajes conversados al día (`bot.limite_mensajes_dia` en `config.yaml`, 80 por defecto); los comandos no cuentan.

**Banco de LLM.** Si un proveedor se agota y E.D.I.T.H. pasa al siguiente, te llega un aviso por Telegram («🔁 groq no está respondiendo… sigo con openai») y otro cuando vuelve el principal.

**Datos.** Todo queda en `data/` (o en `EDITH_DATOS`): `cuentas.db` con las cuentas, `usuarios/<id de Telegram>/` con la base y los archivos de cada persona, y `clave_maestra` si no definiste `EDITH_CLAVE_MAESTRA`. **Guarda una copia de esa clave** (el asistente la pone en tu `.env`): sin ella hay que volver a conectar Moodle. Si antes usabas E.D.I.T.H. solo, al arrancar tus entregas, tu plan y tu Moodle pasan a tu cuenta automáticamente.

### Vigilancia de Moodle

Para cada estudiante con Moodle conectado, E.D.I.T.H. revisa el calendario de Moodle cada 15 minutos dentro de su horario de avisos (6:00 a. m. a 10:00 p. m. por defecto) y avisa por Telegram cuando:

- un profesor publica una tarea nueva (y si aún no abre, desde cuándo se puede entregar),
- se abre una tarea o un quiz que estaba cerrado,
- cambia la fecha de una entrega,
- una entrega está a 72, 24 o 3 horas de vencer (cada aviso se envía una sola vez),
- el profesor sube contenido nuevo al curso (diapositivas, lecturas…) con la opción "notificar cambio de contenido".

Además, **todo lo que hay en Moodle queda planeado solo**: cada tarea o quiz sin entregar se registra como entrega, se divide en subtareas y recibe bloques de estudio, sin tener que dictárselo. Si cambia la fecha se reacomoda y, al entregar, se liberan sus bloques. Se desactiva con `moodle.planear_entregas: false` en `config.yaml`. `/pendientes` muestra todo agrupado por materia, con la fecha y lo que falta para cada entrega.

Consulta el estado de cada entrega: **lo que ya entregaste no se recuerda** (en quices, cuenta un intento terminado), avisa si dejaste un borrador sin enviar y confirma una vez cuando detecta que entregaste. Si el acceso deja de funcionar (por ejemplo, porque cambiaste la contraseña), avisa una vez y pide reconectar con `/conectar_moodle`.

Moodle no ofrece webhooks para estudiantes, por eso la detección es por consulta periódica, un estudiante a la vez para no saturar la plataforma. La primera revisión solo registra lo existente, sin anunciarlo como nuevo. La consulta en lenguaje natural ("¿qué tengo en Moodle esta semana?", "¿ya entregué el plan de pruebas?") también funciona desde el chat.

### Despliegue

Lo normal es tenerlo en tu propio computador con `python servicio.py activar`. Si quieres que responda aunque apagues el computador, la guía [`docs/despliegue.md`](docs/despliegue.md) explica las opciones gratuitas en la nube: una VM gratuita de Google Cloud con Docker (`docker compose up -d --build`) o Render en modo webhook (`EDITH_WEBHOOK_URL`). Solo puede correr una copia del bot por token: si lo subes a la nube, apágalo en tu computador.

### Sin Telegram

```bash
python main.py --consola
```

Abre una conversación con el agente en la terminal, para un solo usuario: usa la base de `config.yaml: base_de_datos` y las credenciales de Moodle de `.env`. Los comandos `/hoy`, `/pendientes`, `/revisar_dia` y `/archivo RUTA` funcionan incluso sin llave de LLM.

## Pruebas

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Las pruebas cubren el planificador (día lleno, entrega imposible, bloques que no caben, tope diario, margen), los skills, la organización de archivos, el bucle del agente con un LLM simulado, los adaptadores de cada proveedor y el banco de respaldo entre proveedores, el bot sin conexión, el registro guiado, la pausa, las rutinas por estudiante, el cifrado del token de Moodle, que los datos de un estudiante no se vean desde otro, el acceso privado, el asistente de instalación y el servicio. Se ejecutan en Linux y en Windows con cada cambio.

Para reentrenar los modelos desde cero y volver a generar las métricas:

```bash
python -m ml.entrenar --regenerar
```

## Estructura

```
├── configurar.py             asistente de instalación: arma el .env haciendo preguntas
├── servicio.py               deja el bot encendido siempre (activar / desactivar)
├── main.py                   arranca el bot (polling o webhook) o la consola
├── arranque.py               ensambla el contexto del modo consola
├── usuarios.py               sesión de cada estudiante: su base, su Moodle y su agente
├── ajustes.py                lee config.yaml y .env
├── config.yaml               reglas del usuario
├── agente/
│   ├── orquestador.py        bucle del agente
│   ├── herramientas.py       esquemas JSON de los skills
│   ├── llm.py                adaptador común y banco de proveedores (Groq, GPT, Claude, Gemini y APIs compatibles)
│   └── prompts.py            prompt de sistema y plantillas
├── skills/                   los cuatro skills y las herramientas de soporte
├── planificador/scheduler.py algoritmo voraz de asignación de bloques
├── integraciones/            Telegram, Google Calendar, calendario local, vigilante
├── ml/                       entrenamiento, modelos guardados y generación de datos
├── notebooks/                datos, predictor y clasificador, ejecutados con salidas
├── db/                       esquema SQL, acceso a datos y cuentas (token cifrado)
├── data/                     pénsum y datasets de arranque
├── docs/encuesta.md          formulario para recolectar datos reales
├── scripts/desplegar_vm.sh   instala y arranca el bot con Docker en una VM
├── Dockerfile, docker-compose.yml   imagen y despliegue con Docker
├── docs/despliegue.md        cómo desplegarlo gratis
└── tests/                    pruebas con pytest
```

## Limitaciones

- Los modelos incluidos están entrenados con datos sintéticos. Sus métricas no valen como resultado final hasta repetir el entrenamiento con la encuesta y con archivos reales.
- Los planes gratuitos (Groq, Gemini…) limitan las solicitudes por minuto y por día. Con un banco de varios proveedores, cuando uno se agota sigue el siguiente; si todos se agotan, funcionan los comandos pero no la conversación.
- La base de datos es SQLite, una por estudiante. Alcanza para una prueba con decenas de estudiantes; para toda la universidad convendría PostgreSQL y varias instancias.
- En el servidor cada estudiante usa el calendario interno; a Google Calendar u Outlook llega con `/calendario` (un archivo que hay que volver a importar cuando cambia el plan), no con sincronización automática.
- Consultar Moodle con el acceso de los estudiantes debería acordarse con la oficina de TI de la universidad antes de un uso abierto.
- El planificador es voraz: encuentra un buen plan, no necesariamente el óptimo.
- Los PDF escaneados sin texto no se pueden clasificar por contenido; en ese caso pregunta.

## Mejoras futuras

- Leer el programa de cada materia y extraer fechas y porcentajes de forma automática.
- Calcular la nota necesaria en lo que falta del semestre y usarla en la prioridad.
- Notas de Moodle: "¿cómo voy en Redes?" y cuánto se necesita en el final.
- Descargar y organizar solo el material que suben los profesores.
- Mensajes de voz.
- Reemplazar el algoritmo voraz por un optimizador con restricciones (OR-Tools).
- Ajustar un modelo de tiempos personal sobre el modelo general.
