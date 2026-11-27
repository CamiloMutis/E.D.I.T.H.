# Despliegue: dejar a E.D.I.T.H. encendido

Esta guía explica cómo poner el bot a funcionar en un servidor para que responda
aunque tu computador esté apagado. No necesitas experiencia previa: copia los
comandos en orden.

> Datos revisados el 26 de noviembre de 2026. Los planes gratuitos cambian sin
> mucho aviso (Oracle, por ejemplo, recortó el suyo en 2026). Antes de crear la
> cuenta, confirma las condiciones en los enlaces de la sección [Fuentes](#fuentes).

## Resumen: ¿qué opción elijo?

| Tu caso | Opción | Costo | ¿Tarjeta? | Datos seguros |
|---|---|---|---|---|
| Usarlo tú, en tu equipo | **C. Tu computador** con `python servicio.py activar` | Gratis | No | Sí (en tu disco) |
| Bot encendido 24/7 gratis | **A. VM gratuita de Google Cloud** (e2-micro) con Docker | Gratis dentro de los límites | Sí (solo verificación) | Sí |
| Más RAM, si consigues cupo | **A'. VM Always Free de Oracle Cloud** | Gratis | Sí (solo verificación) | Sí |
| No tienes tarjeta de crédito | **B. Render gratis** en modo webhook | Gratis | No | **No**: se borran al reiniciar |

Lo que necesita el bot: Python 3.12, unos **310 MB de RAM** en funcionamiento normal
y picos de unos **500 MB** cuando reentrena los modelos (domingos 20:00), disco
persistente para `data/` y salida a internet por HTTPS hacia Telegram, los
proveedores de LLM que uses (Groq, OpenAI, Anthropic, Google…) y el Moodle de
la universidad. No necesita puertos abiertos en
modo *polling* (el normal).

**Regla de oro:** solo puede haber **una** copia del bot corriendo con el mismo
token. Si lo dejas en el servidor, detenlo en tu computador (y al revés).

---

## Antes de empezar (todas las opciones)

1. **Crea el bot en Telegram.** Abre [@BotFather](https://t.me/BotFather), envía
   `/newbot`, elige nombre y usuario. Copia el token (`123456:ABC...`): va en
   `TELEGRAM_BOT_TOKEN`.
2. **Llave de Groq** (el LLM principal, gratuito): créala en
   <https://console.groq.com/keys> → `GROQ_API_KEY`.
3. **Respaldos (opcional, recomendado):** las llaves de los proveedores que
   quieras usar cuando Groq se quede sin cuota (`OPENAI_API_KEY`,
   `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`…) y el orden en `LLM_PROVIDERS`, por
   ejemplo `LLM_PROVIDERS=groq,openai,anthropic`. Ver `.env.example`.
4. **Tu ID de Telegram** (para ser administrador): escríbele a
   [@userinfobot](https://t.me/userinfobot); te responde con un número →
   `TELEGRAM_ADMIN_ID`.
5. **Clave maestra de cifrado.** Con ella se cifran los tokens de Moodle de los
   estudiantes. Genérala una sola vez:

   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   (Si no tienes `cryptography` instalado, corre el comando dentro del `.venv`
   del proyecto: `.venv/bin/python -c "..."`.) Va en `EDITH_CLAVE_MAESTRA`.

   > **Guárdala en un lugar seguro (por ejemplo, un gestor de contraseñas).**
   > Si la pierdes o la cambias, los tokens de Moodle guardados ya no se pueden
   > descifrar y **cada estudiante tendrá que volver a conectar su Moodle**.
   > Si no la defines, la app genera una y la guarda en `/datos/clave_maestra`;
   > en ese caso, respalda el volumen.

6. **Archivo `.env`.** Lo más fácil: `python configurar.py` te pregunta todo y lo
   escribe. A mano: cópialo desde `.env.example` y complétalo. Ejemplo mínimo
   (sin comillas y sin espacios alrededor del `=`):

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456789:AAH...
   TELEGRAM_ADMIN_ID=987654321
   GROQ_API_KEY=gsk_...
   # Opcional: banco de respaldo, en el orden que prefieras
   LLM_PROVIDERS=groq,openai
   OPENAI_API_KEY=sk-...
   OPENAI_MODEL=nombre-del-modelo
   EDITH_CLAVE_MAESTRA=pon_aqui_la_clave_generada=
   MOODLE_URL=https://pregradov.usc.edu.co
   # Quién usa el bot: privado (solo tú, por defecto), invitacion o publico.
   # Para un servidor abierto a los estudiantes, publico (o invitacion + código).
   EDITH_ACCESO=publico
   EDITH_CODIGO_INVITACION=
   ```

   Cada estudiante entrega **su propio** usuario y contraseña de Moodle dentro
   del chat; el bot solo guarda un token, cifrado.

---

## Opción A (recomendada para 24/7): VM gratuita de Google Cloud + Docker

**Por qué esta:** Google da **una VM e2-micro gratis cada mes** (1 GB de RAM),
30 GB de disco persistente y no apaga las máquinas por estar "inactivas". Es
estable y predecible. Requisitos y límites:

- Necesitas una cuenta de facturación con tarjeta (solo para verificar
  identidad; puede aparecer un cobro pequeño que luego se reversa).
- La VM gratuita solo existe en **us-west1 (Oregón), us-central1 (Iowa) o
  us-east1 (Carolina del Sur)**.
- Incluye 1 GB al mes de tráfico de salida; el bot usa bastante menos, pero
  configura una alerta de presupuesto por si acaso.
- 1 GB de RAM es justo: el script crea 2 GB de *swap* para los picos.

### A.1 Crear la máquina

1. Entra a <https://console.cloud.google.com>, crea un proyecto (por ejemplo
   `edith`) y activa la facturación.
2. **Facturación → Presupuestos y alertas:** crea un presupuesto de 1 USD con
   alertas. Así te enteras si algo deja de ser gratis.
3. **Compute Engine → Instancias de VM → Crear instancia:**
   - Nombre: `edith`
   - Región: `us-east1` (la más cercana a Colombia de las tres gratuitas).
   - Tipo de máquina: **e2-micro** (serie E2).
   - Disco de arranque: **Ubuntu 24.04 LTS**, tipo **Disco persistente estándar**
     (no "balanceado" ni SSD: esos no son gratis), 30 GB.
   - Firewall: no marques HTTP/HTTPS (en modo polling no hace falta).
4. Crea la instancia y pulsa el botón **SSH** que aparece a su lado. Se abre
   una terminal en el navegador dentro del servidor.

### A.2 Probar que el servidor llega a Moodle

Algunas universidades bloquean conexiones desde el extranjero. Antes de seguir,
en la terminal SSH:

```bash
curl -I --max-time 15 https://pregradov.usc.edu.co/login/index.php
curl -I --max-time 15 https://api.telegram.org
curl -I --max-time 15 https://api.groq.com
```

Si cada uno responde con una línea `HTTP/2 200`, `HTTP/1.1 303`, `HTTP/2 404` o
similar, **hay conexión** (cualquier código HTTP sirve; lo que importa es que
responda). Si Moodle da `Connection timed out` o `Connection refused`, la
universidad está bloqueando la IP del servidor: el bot funcionará, pero sin
Moodle. En ese caso usa la Opción C (un equipo en Colombia).

### A.3 Instalar y arrancar

```bash
git clone https://github.com/CamiloMutis/E.D.I.T.H..git ~/edith
cd ~/edith
bash scripts/desplegar_vm.sh
```

La primera vez el script instala Docker, crea el swap y se detiene pidiéndote
el `.env`. Llénalo:

```bash
nano ~/edith/.env        # pega tus valores; guarda con Ctrl+O, Enter y sal con Ctrl+X
```

Y vuelve a correrlo:

```bash
bash ~/edith/scripts/desplegar_vm.sh
```

Construir la imagen tarda unos minutos la primera vez. Luego el bot queda
corriendo en segundo plano y **arranca solo** si la VM se reinicia
(`restart: unless-stopped`).

Si prefieres hacerlo a mano, el script equivale a:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER     # cierra y abre la sesión SSH después de esto
cd ~/edith && cp .env.example .env && nano .env
docker compose up -d --build
```

### A.4 Ver registros, reiniciar y actualizar

```bash
cd ~/edith
docker compose logs -f           # registros en vivo (Ctrl+C para salir)
docker compose ps                # ¿está corriendo?
docker compose restart           # reiniciar
docker compose down              # detener (los datos se conservan)
```

Para actualizar a la última versión del código:

```bash
cd ~/edith
git pull
docker compose up -d --build
```

(o simplemente `bash scripts/desplegar_vm.sh`, que hace lo mismo).

### A.5 Respaldos del volumen `/datos`

Todo lo importante (cuentas, datos de cada estudiante, clave maestra si la app
la generó) está en el volumen de Docker `edith_datos`. Para respaldarlo:

```bash
mkdir -p ~/respaldos
docker run --rm -v edith_datos:/datos:ro -v ~/respaldos:/respaldo alpine \
  tar czf /respaldo/edith-$(date +%F).tar.gz -C /datos .
```

Para hacerlo todos los días a las 3:00 a. m. y conservar solo los últimos 14,
abre `crontab -e` y agrega estas líneas (en cron, `%` se escribe `\%`):

```cron
0 3 * * * docker run --rm -v edith_datos:/datos:ro -v $HOME/respaldos:/respaldo alpine tar czf /respaldo/edith-$(date +\%F).tar.gz -C /datos . >/dev/null 2>&1
30 3 * * * find $HOME/respaldos -name 'edith-*.tar.gz' -mtime +14 -delete
```

Descarga de vez en cuando un respaldo a tu computador (en la terminal SSH del
navegador: engranaje → *Descargar archivo*). Un respaldo que solo vive en el
mismo servidor no te salva si borras la VM.

Para **restaurar** un respaldo:

```bash
cd ~/edith && docker compose down
docker run --rm -v edith_datos:/datos -v ~/respaldos:/respaldo alpine \
  sh -c "rm -rf /datos/* && tar xzf /respaldo/edith-AAAA-MM-DD.tar.gz -C /datos && chown -R 1000:1000 /datos"
docker compose up -d
```

### A'. Alternativa: Oracle Cloud Always Free

Oracle regala más recursos: hasta **2 OCPU y 12 GB de RAM** en máquinas Arm
(Ampere A1) o dos VM AMD de 1 GB, con 200 GB de disco. Los pasos son los
mismos de A.2 a A.5 (imagen Ubuntu 24.04; el Dockerfile funciona en Arm). Pero
ten en cuenta:

- También pide tarjeta para verificar la cuenta.
- En 2026 Oracle **redujo** el límite de Ampere de 4 OCPU/24 GB a 2 OCPU/12 GB
  sin anunciarlo; a quienes se pasaban les apagaron las máquinas.
- Con frecuencia no hay cupo ("Out of capacity") para crear máquinas Ampere.
- Oracle puede **recuperar (borrar) máquinas gratuitas inactivas**: si en 7
  días el uso de CPU (percentil 95), de red y de memoria queda por debajo del
  20 %. Un bot como este casi no usa CPU, así que corre ese riesgo. Pasar la
  cuenta a "Pay As You Go" (sigue sin cobrar si te quedas en los límites
  gratuitos) suele evitarlo, pero revisa las condiciones vigentes.
- Por defecto, el firewall de Oracle bloquea los puertos de entrada; en modo
  polling no importa.

---

## Opción B (sin tarjeta): Render gratis en modo webhook

Render permite correr un contenedor Docker gratis sin tarjeta, pero **está
pensado para pruebas, no para producción**. Sé consciente de lo que implica:

- **Se duerme** tras 15 minutos sin recibir peticiones HTTP y tarda cerca de un
  minuto en despertar. Mientras duerme, **no revisa Moodle ni envía
  recordatorios**.
- **El disco es efímero:** cada reinicio, redepliegue o "siesta" borra los
  archivos. Las cuentas, los datos de los estudiantes y sus conexiones a
  Moodle **se pierden**; tendrán que registrarse otra vez. Render puede
  reiniciar el servicio gratuito cuando quiera.
- 512 MB de RAM: alcanza para el uso normal (~310 MB), pero el reentreno
  semanal llega a ~500 MB y puede hacer que Render lo reinicie (y pierda datos).
- 750 horas gratis al mes por cuenta: alcanzan para un solo servicio despierto
  todo el mes (un mes tiene 720-744 horas).

Por eso, el bot debe correr en **modo webhook** (Telegram le envía cada mensaje
por HTTP, lo que además lo despierta) y conviene un "despertador" externo que
lo visite cada pocos minutos.

### B.1 Pasos

1. Sube el código a tu cuenta de GitHub (un *fork* del repositorio sirve).
2. Entra a <https://render.com>, regístrate con GitHub y elige
   **New → Web Service**, conecta el repositorio.
   - Language/Runtime: **Docker** (Render detecta el `Dockerfile`).
   - Instance type: **Free**.
   - Health check path: `/salud`.
3. En **Environment** agrega las variables del `.env` (una por una, sin
   comillas) y además:

   ```
   EDITH_WEBHOOK_URL=https://TU-SERVICIO.onrender.com
   ```

   (La URL exacta aparece arriba en la página del servicio.) Render define
   `PORT` solo; la app escucha en ese puerto.
4. Crea el servicio y espera a que el registro diga que está en línea.
5. **Despertador:** crea una cuenta gratis en <https://cron-job.org> (o
   <https://uptimerobot.com>) y programa una petición `GET` a
   `https://TU-SERVICIO.onrender.com/salud` cada 10 minutos. Así no se duerme.

Si eliges esta opción, define `EDITH_CLAVE_MAESTRA` en las variables: si no,
cada reinicio generaría una clave nueva.

### Otras plataformas revisadas (y por qué no se recomiendan)

| Plataforma | Situación (nov. 2026) |
|---|---|
| Koyeb | 1 instancia gratis de 512 MB y 0,1 vCPU; se duerme tras 1 hora sin tráfico y no se puede desactivar. Puede pedir tarjeta para verificar. Disco no persistente. |
| Fly.io | Sin plan gratuito estable para cuentas nuevas (prueba corta o crédito pequeño con tarjeta); las condiciones han cambiado varias veces. |
| Railway | Solo un crédito de prueba o de 1 USD al mes; no alcanza para un bot 24/7. |
| Hugging Face Spaces | Bloquea las conexiones a `api.telegram.org`: el bot no puede hablar con Telegram. |
| PythonAnywhere (gratis) | Solo permite salir a una lista blanca de sitios (el Moodle de la universidad no está) y no tiene tareas "siempre encendidas" en el plan gratuito. |

---

## Opción C: tu computador o una Raspberry Pi (la más simple)

Ideal para la presentación en clase, o para dejarlo 24/7 en un equipo viejo o
una Raspberry Pi en casa. Además, al estar en Colombia, no hay problema de
bloqueo de IP con Moodle.

```bash
git clone https://github.com/CamiloMutis/E.D.I.T.H..git ~/edith
cd ~/edith
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python configurar.py        # te pregunta todo y escribe el .env
```

Para que quede corriendo en segundo plano y se reinicie solo si falla (Linux,
Windows y macOS, sin permisos de administrador):

```bash
python servicio.py activar --siempre   # --siempre (Linux): corre aunque no inicies sesión
python servicio.py estado
python servicio.py registro
python servicio.py desactivar          # lo apaga y quita el arranque automático
```

Si el equipo tiene Docker, también puedes usar `docker compose up -d --build`
igual que en la Opción A. En Windows, `python servicio.py activar` crea una tarea
programada que lo arranca al iniciar sesión, sin ventana.

Ojo: el computador no debe suspenderse (desactiva la suspensión automática).

---

## Verificación

1. En Telegram, abre tu bot y envía `/start`. Debe responder y guiarte en el
   registro.
2. Envía `/estado`: muestra si Moodle y el LLM están funcionando.
3. Revisa los registros:
   - Docker: `docker compose logs -f`
   - Servicio en tu equipo: `python servicio.py registro`
   - Render: pestaña **Logs** del servicio.

   Debes ver líneas como `LLM en uso: groq (...)` y `E.D.I.T.H. en línea`.
4. En modo webhook, abre `https://TU-SERVICIO.onrender.com/salud` en el
   navegador: debe mostrar `ok`.

### Problemas frecuentes

| Síntoma | Causa | Solución |
|---|---|---|
| `Conflict: terminated by other getUpdates request` | Hay **dos copias** del bot con el mismo token (por ejemplo, tu PC y el servidor). | Deja solo una: `python servicio.py desactivar` o `docker compose down` en la que sobra. Si cambiaste de webhook a polling, reinicia; la app borra el webhook al arrancar en polling. |
| El bot no responde nada | Contenedor caído o token mal copiado. | `docker compose ps` y `docker compose logs --tail 50`. Revisa `TELEGRAM_BOT_TOKEN` en `.env` y luego `docker compose up -d`. |
| Errores de conexión con Moodle (`timed out`, `ConnectError`) | La universidad bloquea la IP del servidor o Moodle está caído. | Prueba `curl -I https://pregradov.usc.edu.co/login/index.php` en el servidor. Si solo falla desde el servidor, usa un equipo en Colombia (Opción C). |
| Respuestas más lentas o diferentes; en el registro aparece `429` o "rate limit" de Groq | Se acabó la cuota gratuita de Groq. | Es normal: la app pasa al siguiente proveedor del banco (`LLM_PROVIDERS`) si tienes alguno configurado. La cuota de Groq se recupera sola. |
| El contenedor se reinicia solo, `Killed` u `OOMKilled` | Falta de memoria (sobre todo en el reentreno del domingo). | En la VM, verifica el swap con `free -h` (el script crea 2 GB). En Render gratis no hay solución: es el límite de 512 MB. |
| Los estudiantes deben volver a conectar Moodle | Cambió o se perdió `EDITH_CLAVE_MAESTRA`, o se perdió el volumen. | Restaura la clave y el respaldo de `/datos`. |
| `permission denied` al usar `docker` | Tu usuario aún no está en el grupo `docker`. | Cierra y abre la sesión SSH, o usa `sudo docker ...`. |

---

## Seguridad y datos personales

- **El archivo `.env` nunca va a Git** (ya está en `.gitignore`) ni dentro de la
  imagen de Docker (está en `.dockerignore`). No lo compartas por chat ni lo
  pegues en capturas. Si un token se filtra, revócalo: en @BotFather con
  `/revoke`, y en las consolas de Groq y Google.
- En el servidor, deja el `.env` con permisos `600` (`chmod 600 .env`; el
  script lo hace).
- El bot maneja datos personales de estudiantes (identificador de Telegram,
  acceso a su Moodle, tareas y horarios). En Colombia eso está cubierto por la
  **Ley 1581 de 2012 (Habeas Data)**: los estudiantes deben saber qué datos se
  guardan y para qué, aceptar ese uso, y poder consultarlos y pedir que se
  borren. El comando **`/olvidarme`** borra todos los datos del estudiante
  que lo envía.
- Solo invita a personas que acepten estas condiciones (usa
  `EDITH_CODIGO_INVITACION` para que no se registre cualquiera).
- Los respaldos contienen datos personales: guárdalos con el mismo cuidado que
  el `.env`.

---

## Fuentes

Consultadas el 26 de noviembre de 2026:

- Google Cloud, niveles gratuitos (e2-micro, regiones, disco, tráfico):
  <https://docs.cloud.google.com/free/docs/free-cloud-features> y
  <https://cloud.google.com/free/docs/compute-getting-started>
- Oracle Cloud, recursos Always Free y reclamación de instancias inactivas:
  <https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm>
- Recorte del plan gratuito de Oracle en 2026 (InfoQ, julio de 2026):
  <https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/>
- Render, plan gratuito (suspensión, disco efímero, 750 horas):
  <https://render.com/docs/free>
- Koyeb, instancia gratuita y escalado a cero:
  <https://www.koyeb.com/docs/faqs/pricing> y
  <https://www.koyeb.com/docs/run-and-scale/scale-to-zero>
- Comparativa de planes gratuitos (septiembre-noviembre de 2026):
  <https://flaviocopes.com/hosting-free-tiers/>
- Fly.io, planes descontinuados: <https://fly.io/docs/about/discontinued-plans/>
- Hugging Face Spaces y Telegram (foro):
  <https://discuss.huggingface.co/t/im-having-trouble-with-the-telegram-api-and-other-services/174473>
- PythonAnywhere, restricciones de cuentas gratuitas:
  <https://www.pythonanywhere.com/pricing/>
