# Instalación y uso de la skill radar-github v0.3.0

Cómo instalar, verificar y revertir la skill `radar-github` en un entorno Hermes.

## Requisitos

- **Python 3.10 o superior.** La skill usa **solo la biblioteca estándar**: no hay
  dependencias que instalar, y `gh` no es necesario.
- Red saliente a `api.github.com`.
- Para **leer** (parse, inspect, dedupe, card) no hace falta ninguna credencial: la
  API pública admite 60 peticiones por hora y solo permite leer.
- Para **escribir** hace falta un token *fine-grained* en la variable de entorno
  `GH_TOKEN` (o `GITHUB_TOKEN`), limitado al repositorio de registro
  (`bobymor1-dev/hermes-component-radar`) con los permisos `Issues: read/write`,
  `Contents: read/write` y `Metadata: read`.

La credencial se lee **solo del entorno**: nunca se pasa como argumento de línea de
comandos, nunca se imprime y nunca se escribe en disco. Los scripts devuelven
únicamente el *nombre* de la variable usada (`token_source`). Un valor con salto de
línea o caracteres no ASCII se rechaza antes de usarlo. **No guardes el token en
este repositorio.**

## Contenido

| Ruta | Qué es |
|---|---|
| `SKILL.md` | Documentación de uso de la skill (v0.3.0). |
| `scripts/radar_github.py` | Lectura: parse, inspect, dedupe, card. |
| `scripts/radar_github_write.py` | Escritura remota: Issue, comentario, cierre e `INVENTARIO.md`. |
| `tests/` | Batería de pruebas (133 pruebas). |
| `tests/fixtures/INVENTARIO.md` | Inventario de ejemplo usado por las pruebas. |
| `SHA256SUMS` | Sumas de integridad de todos los ficheros anteriores. |
| `INSTALL.md` | Este documento. |

## Instalación

1. Comprueba la integridad del contenido descargado:

   ```bash
   sha256sum -c SHA256SUMS
   ```

2. Ejecuta la batería de pruebas **antes** de instalar:

   ```bash
   python3 -m unittest discover -s tests -q
   ```

   Debe terminar en `OK` con 133 pruebas.

3. Copia el árbol completo al directorio de skills del perfil de Hermes, como
   `radar-github/`:

   ```bash
   cp -r SKILL.md SHA256SUMS scripts tests <PERFIL>/skills/radar-github/
   ```

   No hay ningún paso de compilación ni de instalación de dependencias. La skill se
   carga en la siguiente sesión de Hermes; `SKILL.md` ya declara la versión 0.3.0.

## Verificación después de instalar

```bash
cd <PERFIL>/skills/radar-github && sha256sum -c SHA256SUMS
cd <PERFIL>/skills/radar-github && python3 -m unittest discover -s tests -q
python3 <PERFIL>/skills/radar-github/scripts/radar_github.py parse "https://github.com/owner/repo"
```

Esperado: sumas en verde, 133 pruebas en verde, y `parse` devolviendo el slug
`owner/repo`.

## Uso

Lectura y preparación (siempre permitido, no modifica nada):

```bash
python3 scripts/radar_github.py parse "URL"
python3 scripts/radar_github.py inspect OWNER REPO
python3 scripts/radar_github.py dedupe OWNER REPO --inventory INVENTARIO.md
python3 scripts/radar_github.py card OWNER REPO --data ficha.json --out ficha.md
```

Escritura remota (requiere credencial y aprobación humana explícita):

```bash
python3 scripts/radar_github_write.py issue-create "Título" --body-file cuerpo.md --candidate OWNER/REPO
python3 scripts/radar_github_write.py issue-comment 7 --body-file nota.md
python3 scripts/radar_github_write.py issue-close 8 --body-file cierre.md --state-reason not_planned
python3 scripts/radar_github_write.py inventory-append --candidate OWNER/REPO --row "| ... |"
```

Sin `--apply`, todas las órdenes de escritura son un *dry-run* que no envía nada
(`inventory-append` hace una lectura para informar del `sha` destino y del campo
`changed`). Añade `--apply` para ejecutar de verdad.

Códigos de salida: `0` hecho · `1` falló · `3` la puerta de duplicados rechazó ·
`4` se aplicó pero no se pudo verificar.

## Reversión

La skill no tiene estado propio: revertir es restaurar la versión anterior del
directorio.

1. Conserva una copia verificada del directorio anterior **antes** de sustituirlo
   (`cp -r` más `sha256sum -c`), o usa la copia que hayas hecho en el paso de
   instalación.
2. Sustituye `<PERFIL>/skills/radar-github/` por esa copia.
3. Verifica la restauración con `sha256sum -c SHA256SUMS` de la copia restaurada. La
   integridad por sha256 es la puerta de éxito de la reversión.

Ninguna operación de la skill publica ni borra nada por sí sola: la escritura remota
solo toca Issues y `INVENTARIO.md` del repositorio de registro, y siempre tras
aprobación humana.

## Límites conocidos

- **Un veredicto no concluyente no es un "no existe".** Si no se puede consultar el
  inventario o los Issues, el resultado es `indeterminate`, nunca `not_found`.
- **La búsqueda de Issues es full-text** y encuentra palabras en el cuerpo de fichas
  ajenas; por eso los resultados se clasifican por título.
- **Solo `inventory-append` es idempotente.** `issue-create` y `issue-comment` no lo
  son: antes de reintentar una escritura dudosa, **lee el estado remoto** (por
  ejemplo, los comentarios del Issue) para no duplicar.
- **Una escritura aplicada pero no confirmada no es un fallo.** El resultado llega
  como `applied: true, verified: false` con un motivo: comprueba el estado remoto
  antes de reintentar.
- **La ausencia de `SECURITY.md` en un candidato no prueba inseguridad**, y un
  precheck documental no equivale a VALIDADO.
- El token de escritura debe rotarse fuera del repositorio; este árbol no contiene
  credenciales ni ficheros de entorno.
