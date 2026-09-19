# Simulación de OpenVLA con Franka Panda en MuJoCo — Bitácora de progreso

[2026-09-04]
Reporte de todo lo trabajado hasta ahora en este proyecto de tesis: simular un modelo OpenVLA (`openvla/openvla-7b`) controlando un brazo robótico Franka Emika Panda dentro de MuJoCo.

## 1. Objetivo

Desplegar `openvla/openvla-7b` en una simulación de MuJoCo con un Franka Panda, para que el modelo reciba una imagen de una cámara simulada + una instrucción en lenguaje natural, y produzca una acción de 7-DoF (`Δx, Δy, Δz, Δroll, Δpitch, Δyaw, gripper`) que mueva el brazo.

## 2. Diagnóstico inicial y decisiones de diseño

- Se partió de un script de ejemplo (`scripts/openvla_test.py`) copiado tal cual de la tarjeta de HuggingFace de OpenVLA, con placeholders sin implementar (`get_from_camera(...)`, `robot.act(...)`).
- Se seleccionó el modelo **Franka Emika Panda** (de `mujoco_menagerie`), que ya trae el gripper (`hand` + `left_finger`/`right_finger` + actuador de pinza) integrado.
- El repo se reorganizó: se eliminó el paquete ROS2 (`setup.py`, `package.xml`), se quitó el FR3, y se movió `franka_emika_panda/` a `robots/franka_emika_panda/` (ruta actual). El script de lanzamiento `mujoco.launch.sh` vive ahora en la raíz del repo.

## 3. Entorno Python

- Se implementó un venv (`.venv/`) en la raíz del repo (Python 3.10.21) — activar siempre con `source .venv/bin/activate` en terminales nuevas.
- Sistema base: Ubuntu 24.04 (glibc 2.39), `nvcc` del sistema = CUDA 12.0 en `/usr/bin/nvcc`.
- **Stack conocido-bueno actual (verificado 2026-09-18):**
  - `torch==2.2.0+cu121` (se **bajó** desde `2.14.0+cu130`; ver más abajo). Flags: `_GLIBCXX_USE_CXX11_ABI=False`, `cp310`.
  - `mujoco==3.12.0` (bindings de Python; antes solo existía el binario standalone en `~/mujoco-3.12.0`).
  - `flash-attn==2.5.5` (**ahora sí instalado y funcionando**, ver abajo).
  - `bitsandbytes==0.46.1` y `accelerate` (necesarios para carga en 4-bit).
- **`flash-attn` (RESUELTO — ahora funciona):** el intento previo con `torch 2.14.0+cu130` había fracasado (`flash-attn==2.8.3.post1` no compilaba contra los headers de ATen de torch 2.14: `at::symint::sizes<T>` ya no existía). La solución fue **bajar torch a `2.2.0+cu121`** (rango probado por OpenVLA) e **instalar un wheel precompilado** de flash-attn en vez de compilar desde fuente:
  ```
  pip install https://github.com/Dao-AILab/flash-attention/releases/download/v2.5.5/flash_attn-2.5.5+cu122torch2.2cxx11abiFALSE-cp310-cp310-linux_x86_64.whl
  ```
  El wheel debe casar con `torch2.2` / `cu12` / `cxx11abiFALSE` / `cp310`. El wheel `cu122` es compatible en runtime con el `cu121` de torch. **Importante: NO compilar desde fuente** — el `nvcc` 12.0 del sistema no puede parsear las cabeceras fortificadas de glibc 2.39 (error `identifier "__builtin_dynamic_object_size" is undefined`; CUDA ≤12.2 es incompatible con glibc ≥2.38).
- **`bitsandbytes` — cuidado con la versión:** la última (`0.50.2`) exige `torch<3,>=2.4`, así que instalarla a secas **subiría torch de vuelta a 2.14** y arrastraría el stack CUDA cu13, rompiendo flash-attn y todo lo demás. La versión más nueva compatible con `torch 2.2.0` es **`0.46.1`** (`torch<3,>=2.2`); desde `0.47` el pin sube a `torch>=2.4`. Regla: antes de instalar cualquier paquete pesado, correr `pip install <pkg> --dry-run` y verificar que `torch` no aparezca en la línea `Would install`.
- **Nota secundaria (numpy):** `numpy` es 2.2.6 pero torch 2.2.0 se compiló contra numpy 1.x (warning `_ARRAY_API not found`). Si aparecen errores de numpy en runtime, `pip install "numpy<2"`.
- **Regresión propia (lección histórica, sigue vigente):** al limpiar archivos huérfanos de un intento previo de flash-attn se ejecutó `rm -rf lib/python3.10/site-packages/nvidia/cu13`, lo cual **rompió `torch`** porque varios paquetes `nvidia-*` (los runtimes CUDA que usa `torch`, no solo los de flash-attn) comparten ese mismo directorio. Se detectó por `libcudart.so.13: cannot open shared object file` y se arregló reinstalando (`pip install --force-reinstall --no-deps`) los paquetes `nvidia-*` con sus versiones exactas. **Nunca** hacer `rm -rf` sobre un directorio de namespace `nvidia/cuXX/`.

## 4. Escena de MuJoCo (`robots/franka_emika_panda/`)

Todo se editó directamente sobre los archivos originales del Panda (no se crearon copias nuevas), por pedido explícito del usuario.

### Cámaras
- **`scene_cam`** (en `scene.xml`): cámara externa fija de tercera persona (estilo BridgeData V2, ya que `unnorm_key="bridge_orig"`). Usa `mode="targetbody" target="link0"` para que MuJoCo calcule la orientación automáticamente (sin necesidad de un quaternion a mano). El usuario originalmente había dejado esta etiqueta mal formada (faltaba el cierre); se corrigió.
- **`wrist_cam`** (en `panda.xml`): cámara de muñeca (eye-in-hand). El usuario la había puesto dentro del body `left_finger` (se movía al abrir/cerrar el gripper); se reubicó dentro del body `hand` (rígido) con `euler="180 0 0"` para que mire hacia los dedos.
- Ambas cámaras renderizan a 224x224 (tamaño de entrada de OpenVLA), verificado con `mujoco.Renderer`.

### Mesa (`robots/franka_emika_panda/table.xml`, nuevo archivo)
- Mesa estática (sin joint): un tablero (`geom` tipo caja) + 4 patas, material `wood` (color sólido marrón, no textura realista).
- Diseñada para que la superficie del tablero quede en `z=0` local del body `table`, es decir, **a la misma altura donde ya estaba la base del robot** — así se evitó tener que mover/editar `panda.xml` para "subir" al robot.
- Se incluye en `scene.xml` con `<include file="table.xml"/>`.

### Suelo
- Se bajó el `geom` del suelo a `pos="0 0 -0.4"` en `scene.xml`, para dejar hueco a las patas de la mesa (que miden 0.4m) sin que se claven en un suelo infinito a `z=0`.

### Cubo (objeto a manipular)
- Body `cube` en `scene.xml`, con `<freejoint/>`, `pos="0.7 0 0.03"`, color verde (`rgba="0 1 0 1"`), tamaño `size="0.025 0.025 0.025"`.
- La posición `x=0.5` no fue arbitraria: se verificó con cinemática directa (`mj_forward` + keyframe `home`) que el gripper del Panda, en su pose de reposo, cae en `x≈0.55, y≈0` — el cubo se colocó justo debajo, dentro del alcance del brazo.

### Bug de la keyframe (`task_start`)
- Al añadir el `<freejoint/>` del cubo, el modelo pasó de 9 a 16 grados de libertad (`nq`). La keyframe original `home` (definida en `panda.xml`) solo cubría las 9 originales; MuJoCo rellenaba las 7 faltantes (posición del cubo) con **ceros** en vez de con la posición definida en el XML, y el cubo aparecía en el origen del mundo en lugar de sobre la mesa.
- Se arregló añadiendo una keyframe nueva y completa en `scene.xml`, `task_start`, con los 16 valores correctos (9 del brazo/gripper + 7 del cubo). El script ahora la busca por nombre (`mujoco.mj_name2id(..., mujoco.mjtObj.mjOBJ_KEY, "task_start")`), no por índice.

## 5. Script (`scripts/openvla_test.py`)

Estado actual, verificado end-to-end:

1. Carga la escena MuJoCo desde `robots/franka_emika_panda/scene.xml` (ruta resuelta con `Path(__file__)`, robusta al directorio de ejecución).
2. Resetea a la keyframe `task_start` y hace `mj_forward`.
3. Crea un `mujoco.Renderer(height=224, width=224)`.
4. `capture_image(data, renderer, camera_name)`: reemplaza el placeholder `get_from_camera(...)`; hace `update_scene` + `render()` + `Image.fromarray(...)`.
5. Carga el modelo en **4-bit** (`BitsAndBytesConfig`, `nf4`, doble cuantización) — el usuario implementó esto basándose en que el paper de OpenVLA reporta desempeño casi idéntico a la versión sin cuantizar. Esto era necesario porque la GPU (RTX 4080 laptop) solo tiene **12GB de VRAM**, insuficiente para los ~14-15GB que pesa el modelo en bf16 sin cuantizar.
6. **Dos bugs de carga corregidos**:
   - `.to("cuda:0")` no es compatible con modelos 4-bit → se cambió a `device_map={"": 0}` dentro de `from_pretrained`.
   - Aun con `device_map`, `transformers==4.40.1` llama internamente a `accelerate.dispatch_model(...)`, que para un `device_map` de un solo dispositivo hace un `model.to(device)` que **sigue rompiendo** con modelos 4-bit (bug conocido de esa combinación de versiones). Se aplicó un monkeypatch acotado al inicio del script: `transformers.modeling_utils.dispatch_model = functools.partial(accelerate.dispatch_model, force_hooks=True)`, que fuerza la ruta segura (basada en hooks) sin necesidad de tocar los paquetes instalados ni arriesgar romper el código custom (`trust_remote_code`) de OpenVLA subiendo la versión de `transformers`.
7. Predicción de acción verificada con salida real:
   ```
   Predicted action: [-0.0235  0.0022  0.0089  0.0292  0.0012  0.0048  0.9961]
   ```
   (7 valores: delta-pose del efector final + gripper, gripper≈1.0 = abierto, coherente con que aún no se ha implementado el control).
8. `unnorm_key="bridge_orig"` verificado como válido: está entre las 25 claves del campo `norm_stats` embebido en `config.json` del checkpoint (se comprobó descargando solo ese archivo, sin bajar los ~14GB de pesos).

## 6. Estado actual — qué funciona de punta a punta

- ✅ Carga de la escena MuJoCo (robot + mesa + cubo + 2 cámaras).
- ✅ Captura de imagen real desde `scene_cam` a 224x224.
- ✅ Carga del modelo OpenVLA-7b en 4-bit (cabe en 12GB de VRAM).
- ✅ `predict_action` corre y devuelve una acción de 7-DoF válida.
- ✅ Bucle de control cerrado (captura→predicción→IK→actuación→step) corriendo end-to-end, con guardado de frames para inspección visual.
- ✅ IK diferencial (Jacobiana amortiguada + espacio nulo) convirtiendo la acción del VLA en objetivos articulares.

## 7. Pendiente (no implementado todavía)

1. **Condición de éxito/timeout** en el bucle de control (hoy corre un número fijo de pasos, `N_STEPS`, sin detectar si ya agarró el cubo o si está claramente perdido).
2. **Mapeo del gripper**: el valor continuo de gripper de OpenVLA (`≈0.996` en todas las corridas hasta ahora) se escala linealmente a `ctrlrange="0 255"`, pero nunca se ha observado que el modelo pida cerrar la pinza — no se ha verificado el mapeo con un caso real de cierre.
3. **Alcance real del cubo vs. encuadre de cámara**: con el cubo en `x=0.8` y la cámara actual, el brazo no converge de forma clara hacia el cubo (ver sección 10). Falta decidir si mover el cubo más cerca del alcance documentado (`x≈0.55`) o seguir ajustando la cámara.

## 8. Lecciones / gotchas importantes para no repetir

- **Nunca** hacer `rm -rf` sobre un directorio compartido de namespace de paquetes NVIDIA (`nvidia/cuXX/`) aunque `pip uninstall` diga que ciertos paquetes se removieron — otros paquetes (incluido `torch`) pueden compartir esos mismos archivos. Usar solo `pip uninstall <paquete>` y verificar con una prueba de import.
- Los archivos `nvidia-*-cu13` (con sufijo) en PyPI suelen ser *stubs* de deprecación de 1.4KB — el paquete real es el que NO lleva el sufijo `-cu13` (p. ej. `nvidia-cuda-nvcc`, no `nvidia-cuda-nvcc-cu13`).
- `openvla/openvla-7b` no tiene un `dataset_statistics.json` separado; las claves válidas de `unnorm_key` viven en el campo `norm_stats` de `config.json`.
- En `transformers==4.40.1` + `bitsandbytes` (modelos 4-bit) + un solo GPU, `device_map` como dict de un solo valor **no es suficiente** para evitar el bug de `.to()`; hace falta forzar `force_hooks=True` en `accelerate.dispatch_model` (ver script para el monkeypatch exacto).
- **`mj_jacSite`/`mj_jac*` necesitan `mj_comPos`, no solo `mj_kinematics`**: el Jacobiano usa `data.cdof`, que solo se llena en `mj_comPos`. Si se solo llama `mj_kinematics` sobre un `MjData` que nunca corrió `mj_comPos`/`mj_forward`, el Jacobiano sale (casi) cero y el IK parece "congelado" sin dar ningún error.
- **La keyframe del cubo se desincroniza fácilmente**: el body `cube` tiene un `pos` en el XML, pero una vez que existe una `<keyframe>` con `qpos` explícito, `mj_resetDataKeyframe` siempre gana — cambiar el `pos` del body sin actualizar el `qpos` de la keyframe dijo un cubo "fantasma" en la posición vieja. Ya pasó dos veces en este proyecto (ver sección 10).
- Al mezclar error de posición (metros) y de orientación (radianes) en un solo vector de error 6D para DLS, un valor atípico de rotación del VLA (p. ej. 0.2 rad cuando lo normal es <0.08) puede dominar la solución de mínimos cuadrados y arrastrar la posición en una dirección no deseada — conviene acotar (`clip`) la magnitud de `Δrot` por paso.

## 9. Cinemática diferencial e IK (sesión 2026-09-06)

### Decisión de método

Se investigaron los métodos clásicos de cinemática diferencial para convertir la acción del VLA (delta-pose del efector) en objetivos articulares:

- **Jacobiana transpuesta**: descartada como método principal (lenta, requiere tuning de ganancia por eje), útil solo como baseline.
- **Pseudo-inversa pura**: descartada por diverger cerca de singularidades.
- **Método elegido: Damped Least Squares (DLS) + proyección al espacio nulo** — $\Delta q = J^+_\lambda \Delta x + (I - J^+_\lambda J)\, k_0 (q_0 - q)$, con $J^+_\lambda = J^T(JJ^T + \lambda^2 I)^{-1}$. El término de espacio nulo aprovecha la redundancia del Panda (7 GDL vs. tarea de 6D) para sesgar la solución hacia la postura `home` sin afectar la pose del efector.
- Se implementó como un lazo CLIK (Closed-Loop IK): 5 iteraciones internas por cada acción del VLA, recalculando el Jacobiano y el error de pose en cada una, sobre un `MjData` de repuesto (`ik_data`), no sobre el estado real de la simulación.

### Validación de la convención del delta de orientación

Antes de implementar el IK, se investigó (con un script aislado, `scripts/test_orientation_delta.py`, y con una consulta real al modelo) cómo interpretar correctamente `Δroll, Δpitch, Δyaw`:

- **De código fuente** (`vlas/openvla/prismatic/vla/datasets/rlds/utils/data_utils.py::relabel_bridge_actions`): para `unnorm_key="bridge_orig"`, el delta de rotación de entrenamiento es una **resta ingenua componente a componente** de los ángulos de Euler del estado (`state[t+1] - state[t]`), no un delta propio de $SO(3)$.
- Se determinó experimentalmente que la elección de secuencia Euler (extrínseca/intrínseca) apenas afecta el resultado (~0.2° de diferencia) para las magnitudes típicas del VLA, pero que **el marco de referencia (mundo vs. propio del gripper) sí importa mucho**: en la pose de reposo del Panda (gripper mirando hacia abajo, lejos de la identidad), aplicar el delta en un marco u otro da ejes de rotación distintos (X/Y intercambiados, yaw invertido).
- Se concluyó (justificado por la convención `EEF XYZ`/`world_vector` de los datasets de Open X-Embodiment, y confirmado como lógicamente inambiguo para la posición) aplicar el delta de orientación como **mapa exponencial en el marco mundo**: $R_{target} = \exp([\Delta\text{roll}, \Delta\text{pitch}, \Delta\text{yaw}]_\times)\, R_{actual}$.
- Una verificación puntual con el modelo real dio una rotación de solo ≈1.7°, insuficiente para discriminar entre hipótesis de marco de forma concluyente — la validación final quedó a cargo del comportamiento del rollout completo.

### Implementación (`scripts/openvla_test.py`)

- Se agregó el site `ee_site` en `panda.xml` (TCP, offset `0.1034` desde la brida) para poder usar `mj_jacSite`.
- Nuevas funciones: `computeJacobian`, `computePoseError`, `rotvecToMat`, `solveDlsIk`.
- El bucle principal ahora hace, por cada uno de `N_STEPS` pasos: captura de imagen → `predict_action` → construir `T_target`/`R_target` → resolver DLS+espacio nulo → escribir `mj_data.ctrl` (7 articulaciones + gripper escalado a `0-255`) → `SUBSTEPS_PER_ACTION` pasos de física (`mj_step`) para que el PD de posición asiente el objetivo.
- Se agregó guardado de cada frame capturado en `scripts/rollout_frames/` para inspección visual (vía `view_image`), ya que no hay visor interactivo en este flujo headless.

### Bug encontrado y arreglado: Jacobiano "congelado"

El primer rollout de 20 pasos mostró al brazo completamente inmóvil (frames idénticos, mismas acciones repetidas del VLA porque veía la misma imagen una y otra vez). Diagnóstico: `solveDlsIk` llamaba a `mujoco.mj_kinematics` pero nunca a `mujoco.mj_comPos`, y `mj_jacSite` depende de `data.cdof` (poblado solo por `mj_comPos`). El Jacobiano usado era básicamente basura, y el único movimiento observado era el pequeño término de espacio nulo tratando de arrastrar el brazo de vuelta a `home`. Con una sola línea (`mj_comPos` después de `mj_kinematics`), el Jacobiano pasó a ser real y el brazo se movió de forma coherente hacia el cubo en un rollout de 20 pasos.

## 10. Cámara nueva + cubo más grande: movimientos erráticos (sesión 2026-09-06, continuación)

El usuario cambió la perspectiva de `scene_cam`, agrandó el cubo (`size="0.05 0.05 0.05"`) y corrió 100 iteraciones; reportó movimientos erráticos y que la posición del cubo no correspondía con el XML.

- **Bug de la keyframe (reaparición)**: el body `cube` tenía `pos="0.8 0 0.03"` pero la keyframe `task_start` seguía con el `qpos` viejo (`0.5 0 0.03`). Arreglado sincronizando la keyframe a `0.8 0 0.03`.
- **Los límites articulares no eran el problema**: se instrumentó el margen a los límites (`joint_lo`/`joint_hi`) y nunca bajó de ~0.48 rad durante el rollout.
- **Hipótesis descartada (marco de orientación)**: se consideró si el problema era la convención mundo-vs-gripper del delta de rotación, pero se descartó con un argumento lógico sólido — la posición (`EEF XYZ`) es inambigua respecto a marco por construcción (resta de dos coordenadas en el mismo marco fijo), y el código de aplicación del delta de orientación no cambió entre la corrida que funcionaba bien y esta, así que no puede explicar una regresión nueva.
- **Causa real encontrada**: el VLA ocasionalmente predice un `Δrot` con norma anómalamente grande (ej. 0.203 rad ≈ 11.6° en yaw, frente a <0.08 rad típico). Como `computePoseError` combina el error de posición (metros) y de orientación (radianes) en un solo vector 6D con peso 1:1, un pico de rotación así domina la solución DLS y desvía la posición como efecto colateral.
- **Pendiente**: aun con el recorte, el brazo no converge claramente hacia el cubo en `x=0.5` con la cámara actual — probablemente el cubo sigue estando fuera de la distribución de entrenamiento del modelo (muy lejos y/o mal encuadrado). Falta decidir si acercar el cubo al alcance documentado (`x≈0.55`) o seguir iterando sobre el encuadre de la cámara.