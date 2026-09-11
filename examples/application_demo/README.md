# Consumidor mínimo de Aurvek

Demostración FastAPI de un usuario local, con el chat nativo incrustado y botones
propios. No contiene lógica de Biografías, acceso a SQLite ni código de proveedores.
El adaptador React/Vite se encuentra en `integrations/applications/sdk/AurvekPanel.jsx`.

1. Copia `config.example.json` a un archivo privado. Sustituye los dominios, IDs de
   prompts/modelo y pagador por los de tu instalación. El host del iframe debe
   estar configurado en el ingress de Aurvek, con HTTPS y separado del host nativo.
   Usa el mismo sitio registrable que el padre para la cookie del iframe.
2. Copia `.env.example` a un archivo privado y configura `AURVEK_CLIENT_SECRET`,
   que se usa sólo en el consumidor. El endpoint de ejemplo devuelve un ejercicio
   sintético sin datos privados; para una herramienta que requiera autenticación,
   configura `credential_env` y su secreto en el entorno de Aurvek.
   `AURVEK_DEMO_LOOPBACK_URL` apunta al puerto privado de Aurvek cuando se ejecutan
   juntos o mediante un túnel SSH local; se conserva el Host del issuer registrado.
   Omítela únicamente si tienes configurado el ingress HTTPS de backend remoto.
3. En el checkout de Aurvek, valida y aplica la configuración mediante el CLI
   existente. El secreto de backend se lee de `AURVEK_CLIENT_SECRET` o se pide sin eco:

   ```sh
   python -m integrations.embed.cli configure /private/practice.json --check
   python -m integrations.embed.cli configure /private/practice.json
   python -m integrations.embed.cli show practice-demo
   ```

4. Inicia el consumidor desde este checkout:

   ```sh
   python -m uvicorn examples.application_demo.app:app --host 127.0.0.1 --port 18880 --no-proxy-headers --env-file /private/practice.env
   ```

   Sirve su origen HTTPS configurado mediante el proxy de desarrollo que utilices.
   El ejemplo admite únicamente conexiones loopback y comprueba Origin en las
   operaciones del navegador; es una fixture local, no un sistema de login público.
   `--no-proxy-headers` conserva la IP de esa conexión local: el middleware no
   debe confundirla con la IP del visitante enviada por el proxy.
   El documento padre usa `Referrer-Policy: strict-origin`: conserva el origen
   necesario para autenticar el POST de bootstrap sin compartir rutas ni consultas.
   `no-referrer` hace que ese formulario envíe `Origin: null` y sea rechazado.
   Las rutas del documento son relativas: también puede servirse bajo un prefijo
   como `/admin/f9-preview/` si el proxy elimina ese prefijo al enviarlo a la demo.

Al abrir la página, el backend provisiona la cuenta de demostración si no existe,
obtiene una delegación, prepara su contexto y abre la conversación. Al navegador
sólo llegan el ticket de bootstrap de un uso y los datos de su perfil. Los botones
cambian de asistente, retoman el anterior, guardan la entrada y usan dictado,
adjuntos, voz y exportaciones del mismo panel. La sección inferior actualiza el
perfil local y consulta fuentes y un endpoint remoto mediante los clientes comunes.
Un email pendiente no se convierte en contacto global verificado ni provoca envíos.

`Abrir en otra pestaña` es una alternativa explícita si el navegador bloquea el
iframe. Abre una pestaña desde el clic, espera el cierre de la actividad del panel
y solicita otro ticket para esa misma conversación y asistente. `/bootstrap`
revalida el contexto de servidor; no reutiliza el ticket consumido ni pone
credenciales en URLs. La pestaña no conserva `opener`. El envío del formulario
no acredita que el chat haya terminado de cargar; sus errores aparecen allí.

Para comprobar otra aplicación, usa otro `app_id`, dominios, prompts y credencial;
puedes cambiar la URL de la herramienta a `/lookup-alternate`. No cambies el runtime
de Aurvek. El alta de cada cuenta nace con saldo personal cero; el patrocinio
configurado paga las operaciones admitidas y no recurre a otro monedero.

`configure` actualiza sólo lo declarado; omitir una herramienta, política o asistente
no borra los existentes. Para retirar acceso usa `enabled: false`, modifica la
política o `python -m integrations.embed.cli disable practice-demo`. Reaplicar la
misma configuración conserva sesiones y presupuesto. Un cambio en el registro de
la app revoca sesiones antiguas. No restaures una base de datos sobre mensajes nuevos.

Para integrar tu producto real, sustituye el miembro fijo por el ID que determine
su sesión autenticada de servidor. Reutiliza `client.py` (requiere `httpx`) y la
biblioteca de navegador; no publiques secretos ni credenciales delegadas en el HTML.
Contrato y ejemplos de llamadas: [guía de integración](../../docs/applications/integration-guide.md).
