# U²-Net para quitar el fondo y detectar objetos sobre la mesa

Este documento resume, con palabras sencillas, todo lo que se hizo en esta sesión de trabajo
(del 27 de septiembre al 5 de octubre de 2026): desde elegir datos y entrenar U²-Net hasta
usarlo en tiempo real con la RealSense y ROS 2, y afinarlo para que encuentre **todos** los
objetos de la mesa.

---

## 1. El objetivo

El robot necesita saber **qué píxeles de la imagen son objetos** que puede agarrar y cuáles
son mesa o fondo. Para eso se usa U²-Net, una red que recibe una imagen RGB y devuelve una
máscara: blanco = objeto, negro = fondo.

Hay una diferencia importante entre dos tareas parecidas:

| Tarea | Qué marca | Dataset típico |
|---|---|---|
| **Saliency** (lo que hace el paper original) | El objeto *más llamativo* de la imagen, uno solo | DUTS-TR |
| **Objectness** (lo que necesita el robot) | *Todos* los objetos sobre la mesa | GraspNet-1Billion |

Por eso el trabajo se hizo en dos etapas: primero se replicó el paper (saliency) y después
se afinó el modelo para objectness.

U²-Net no separa un objeto de otro por sí sola: da una única máscara con todos juntos. La
separación en objetos individuales se hace después, con componentes conexas (y la
profundidad de la cámara).

---

## 2. Preparación del entorno

### Instalación de albumentations

`albumentations` es la librería que hace las transformaciones aleatorias de las imágenes
(recortes, giros, cambios de color). Al instalarla normalmente quería traer
`opencv-python-headless`, que habría **pisado** el OpenCV completo que ya había en el `.venv`
(ambos se instalan como `cv2`). Para evitarlo se instaló sin dependencias:

```bash
pip install --no-deps albumentations "albucore==0.0.24"
pip install scipy simsimd stringzilla
```

Versión resultante: **albumentations 2.0.8**. Se comprobó que `cv2` seguía intacto.

### Arreglos previos en el código

- **`u2net.py`** tenía al final un bloque de prueba que creaba el modelo y lo pasaba a la GPU.
  Ese código se ejecutaba cada vez que otro archivo hacía `import u2net`, reservando memoria
  de la GPU sin necesidad. Se desactivó.
- **`u2net_train.py`** construía la ruta de las imágenes concatenando la carpeta dos veces,
  así que la lista de imágenes salía siempre vacía. También dependía de desde qué carpeta se
  ejecutara el script. Ahora las rutas se calculan a partir de la ubicación del propio archivo.

---

## 3. Etapa 1 — Entrenamiento base con DUTS-TR

### Los datos

**DUTS-TR** se descargó en `datasets/DUTS-TR/`: **10 553 pares** de imagen y máscara.
Se separa un 5 % para validación (527 imágenes) siempre con la misma semilla, para que todas
las ejecuciones se comparen sobre las mismas imágenes.

### Cómo se cargan los datos (`SaliencyDataset`)

Para cada imagen:

1. Se busca su máscara por el **nombre del archivo** (misma base, otra carpeta).
2. Se lee la imagen y se pasa de BGR (como la lee OpenCV) a RGB (como espera la red).
3. La máscara se convierte a valores exactos 0 o 1 (`> 127`), que es lo que necesita la
   función de pérdida.
4. Imagen y máscara pasan **juntas** por las transformaciones, para que reciban exactamente
   el mismo recorte y el mismo giro.

### Transformaciones (data augmentation)

| Modo | Para qué etapa | Qué hace |
|---|---|---|
| Normal (paper) | DUTS-TR | Redimensiona a 320, recorta 288 al azar, espejo horizontal |
| `--strong-aug` | Mesa / GraspNet | Recorte aleatorio de tamaño variable, giro ±15°, cambios de brillo y color, ruido, desenfoque de movimiento |

Nunca se usa espejo vertical: en una mesa los objetos siempre están apoyados abajo, y voltear
la imagen enseñaría algo que no ocurre en la realidad.

Un detalle encontrado al probarlo: el ruido gaussiano por defecto de albumentations 2.x es tan
fuerte que destruía la imagen. Se bajó a un nivel suave.

### El modelo

Se comprobó que la implementación propia de U²-Net tiene **44.0 millones de parámetros**,
exactamente lo que dice el paper. Esto confirma que los bloques RSU están bien programados.

La red devuelve 7 salidas: la final (`sup0`) y 6 laterales. Las laterales solo sirven para
entrenar mejor (*deep supervision*); en el uso real solo se usa la final.

Los pesos oficiales `u2net.pth` **no** se pueden cargar en esta versión porque las capas
tienen otros nombres. Por eso el modelo se entrena desde cero.

### La función de pérdida

Para cada una de las 7 salidas se suma:

- **BCE**: castiga cada píxel mal clasificado.
- **IoU**: castiga que la forma completa de la máscara no coincida.

La combinación da mejores bordes que solo BCE (el paper original solo usa BCE).

### Optimizador y velocidad de aprendizaje

| Elemento | Paper original | Lo que se usó |
|---|---|---|
| Optimizador | Adam | **AdamW**, sin *weight decay* en bias ni BatchNorm |
| Learning rate | 1e-3 fijo | 1e-3 con **calentamiento** de 1 época y luego **bajada en coseno** hasta 1e-6 |
| Precisión | float32 | **bf16** (mixta): más rápida y menos memoria en la RTX 4080 |
| Estabilidad | — | Recorte de gradiente a 1.0 |
| Parada | número fijo de iteraciones | **Parada temprana** si el IoU de validación no mejora |

El learning rate se ajusta en cada iteración, no en cada época.

### Pruebas que se hicieron antes de entrenar en serio

1. **Una sola pasada** con el modelo sin entrenar: la pérdida inicial (~1.42 por salida,
   ~10.1 en total) era la esperada para una red que todavía adivina al azar.
2. **Prueba de sobreajuste**: entrenar con solo 14 imágenes hasta memorizarlas. Llegó a
   IoU 0.817 sobre esas imágenes, lo que demuestra que el bucle de entrenamiento aprende de
   verdad.
3. **Memoria**: entrenando con batch 12 usaba 5.4 GB de los 12 GB de la GPU, así que se podía
   subir el batch a 20–24.

### Resultados

Se entrenaron dos modelos, `runs/u2net/v1` y `runs/u2net/v2`. El **v2** llegó a:

| IoU validación | Dice | MAE |
|---|---|---|
| **~0.891** | ~0.931 | ~0.030 |

Es un buen modelo de *saliency*.

---

## 4. Reanudar un entrenamiento cortado

Al principio, cargar `last.pth` solo recuperaba los pesos. Se perdían el estado del
optimizador, la curva del learning rate, el número de época y el mejor IoU; y lo peor: la
primera época tras reanudar **sobrescribía `best.pth`** aunque fuera peor.

Ahora hay dos opciones distintas:

| Opción | Qué recupera | Cuándo usarla |
|---|---|---|
| `--resume ruta/last.pth` | Todo: pesos, optimizador, learning rate, época, mejor IoU | Continuar un entrenamiento que se cortó |
| `--weights ruta/best.pth` | Solo los pesos | Empezar un entrenamiento **nuevo** desde un modelo ya entrenado (fine-tuning) |

Se comprobó que un entrenamiento cortado y reanudado da **exactamente** los mismos learning
rates que uno sin cortes.

También se corrigió un error: `last.pth` guardaba el mejor IoU con una época de retraso.

Ejemplo:

```bash
python u2net_train.py --resume runs/u2net/v2/last.pth --epochs 100
```

---

## 5. Uso en tiempo real con la RealSense y ROS 2

### El nodo `u2net_bgrm`

Archivo: `src/intel_realsense/intel_realsense/u2net_bgrm.py`.

Qué hace con cada imagen de la cámara:

1. Recibe la imagen a color y la profundidad, **sincronizadas** entre sí.
2. Pasa la imagen por U²-Net y obtiene la máscara de objetos.
3. Descarta los píxeles sin profundidad válida.
4. Convierte esos píxeles a puntos 3D usando los parámetros de la cámara.
5. Calcula el **plano de la mesa** con RANSAC usando solo los puntos que la red marcó como
   fondo. (El nodo anterior usaba todos los puntos, y un objeto grande podía inclinar el
   plano.)
6. Separa los objetos en grupos (componentes conexas) y numera cada uno.
7. Publica los resultados.

| Topic | Contenido |
|---|---|
| `/u2net_bgrm/foreground/mask` | Máscara en blanco y negro |
| `/u2net_bgrm/foreground/image_raw` | Imagen solo con los objetos |
| `/u2net_bgrm/foreground/points` | Nube de puntos con el número de objeto de cada punto |
| `/u2net_bgrm/foreground/plane` | Ecuación del plano de la mesa |

Los dos últimos tienen exactamente el mismo formato que los del nodo `irs_node`, así que
`superdec_node` puede usarlos sin cambios.

Detalles importantes:

- **No se usa `cv_bridge`**: en este entorno falla con NumPy 2. Las imágenes se convierten a
  mano.
- La cámara envía RGB y la red espera RGB, así que no se convierte nada antes de inferir.
- La máscara se agranda a la resolución original **antes** de convertirla en blanco/negro;
  al revés, los bordes salen dentados.
- El parámetro `plane_clearance` (por defecto 0) permite exigir además que el objeto esté por
  encima de la mesa. Útil como red de seguridad.

### Velocidad medida

**~13–14 ms por imagen (~70–77 FPS)** en la RTX 4080 con bf16.

### Cómo lanzarlo

El orden de activación importa: primero ROS, luego el paquete, y **el entorno virtual al
final**, para que su Python sea el que se use.

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
source .venv/bin/activate
ros2 run intel_realsense u2net_bgrm --ros-args -p weights:=runs/u2net/vft1/best.pth
```

### Problema de compilación que apareció (y cómo se resolvió)

`colcon build --symlink-install` fallaba con:

```
error: option --uninstall not recognized
```

**Causa:** quedaban restos de una instalación anterior en modo desarrollo
(`build/intel_realsense/intel_realsense.egg-info` y un `intel-realsense.egg-link` dentro de
`install/`). Eso hacía que colcon pidiera una desinstalación que la versión nueva de
setuptools ya no soporta.

**Solución:** apartar esos dos archivos y volver a compilar.

**Efecto secundario:** mientras el estado estaba roto, el ejecutable apuntaba al Python del
sistema (que no tiene torch) y `ros2 run` fallaba con `No module named 'torch'`. Tras la
compilación limpia apunta otra vez al Python del entorno virtual.

Si vuelve a pasar, un apaño rápido es:

```bash
export PYTHONPATH="$PWD/.venv/lib/python3.12/site-packages:$PYTHONPATH"
```

---

## 6. Etapa 2 — Afinar para objectness con GraspNet-1Billion

### Por qué no TO-vanilla

Se revisó el contenido de `datasets/TO-vanilla/`. Resultó que **no son imágenes**, sino
**nubes de puntos** 3D (es el dataset de SuperDec): coordenadas, color y etiqueta de objeto
por punto. U²-Net necesita imágenes 2D con su máscara, así que no se podía usar directamente.

### Por qué GraspNet-1Billion sí

Se revisó la copia en `datasets/GraspNet-1Billion/`:

- **100 escenas** de mesas con varios objetos, cada una con **256 vistas**.
- Tiene versión `realsense/` (el **mismo sensor** que usa el robot) y `kinect/`.
- `rgb/` contiene imágenes reales de 1280×720.
- `label/` contiene, para cada píxel, el número del objeto, y **0 para la mesa**.

Por tanto, la máscara de objectness es simplemente `label > 0`. Es justo el tipo de imagen
que verá el robot.

### Preparación de los datos (`scripts/graspnet_export.py`)

El script:

1. Toma **1 de cada 8 vistas**: las 256 vistas de una escena son casi iguales, y usarlas
   todas solo haría el entrenamiento más lento sin enseñar nada nuevo.
2. Reduce las imágenes a 640×360.
3. Guarda la imagen, la máscara y el mapa de objetos en el formato que ya entiende
   `SaliencyDataset`.
4. Separa **por escena**: escenas 0–89 para entrenar y 90–99 para validar. Nunca se mezclan
   vistas de una misma escena entre entrenamiento y validación; si se hiciera, el resultado
   saldría artificialmente alto.
5. Genera imágenes de vista previa con la máscara en rojo para revisarlas a ojo.

```bash
python scripts/graspnet_export.py --stride 8
```

Resultado: **2 880 imágenes de entrenamiento y 320 de validación**. Los objetos ocupan de
media un 24–30 % de la imagen, muy parecido a DUTS-TR, así que no hay desequilibrio de clases.

Se decidió **no mezclar DUTS-TR** durante este afinado: sus máscaras marcan un único objeto,
lo cual contradice la tarea de marcar todos.

### Nueva métrica: recuperación de objetos (`obj_recall`)

El IoU global puede ser alto aunque la red se olvide de los objetos pequeños, que son
justamente los que el robot tiene que agarrar. Por eso se añadió una métrica que cuenta:

> De todos los objetos que hay en la mesa, ¿qué fracción encontró la red?

Un objeto cuenta como encontrado si la máscara cubre más de la mitad de sus píxeles. Esta
métrica aparece en la consola y en la columna `val_obj_recall` del CSV.

### Cambios en `u2net_train.py`

- `--val-image-dir` y `--val-mask-dir`: permiten dar una carpeta de validación separada
  (necesario para el reparto por escena).
- `--instance-dir`: carpeta con el número de objeto de cada píxel, para calcular
  `obj_recall`.

### Comando de afinado

```bash
cd src/intel_realsense/intel_realsense
D=../../../datasets/graspnet-obj
python u2net_train.py \
  --image-dir $D/train/images --mask-dir $D/train/masks \
  --val-image-dir $D/val/images --val-mask-dir $D/val/masks \
  --instance-dir $D/val/instances \
  --weights ../../../runs/u2net/v2/best.pth \
  --lr 1e-4 --warmup-epochs 0.5 --epochs 30 --patience 8 \
  --strong-aug --batch-size-train 20 --batch-size-val 8 \
  --version ft1
```

Se usa `--weights` (no `--resume`) porque es un entrenamiento nuevo con su propia curva de
learning rate, y un learning rate **10 veces menor** para no destruir lo ya aprendido.

### Resultados

Todas las cifras son sobre las escenas de validación de GraspNet (90–99), que la red nunca vio
durante el afinado.

| Modelo | IoU | Dice | MAE | **Objetos encontrados** |
|---|---|---|---|---|
| v2 (solo saliency, sin afinar) | 0.597 | 0.725 | 0.122 | **71.1 %** |
| Tras 50 iteraciones de afinado (prueba) | 0.721 | 0.830 | 0.097 | 89.8 % |
| **vft1, época 1** | 0.828 | 0.903 | 0.062 | 98.0 % |
| **vft1, mejor IoU (época 39)** | **0.911** | **0.953** | **0.028** | **99.3 %** |
| vft1, mejor `obj_recall` (épocas 12–15) | ~0.90 | ~0.95 | ~0.030 | **99.6 %** |

Lo que muestran estos números:

1. El modelo de saliency, aunque era bueno en su tarea, **se dejaba casi un 30 % de los
   objetos** de la mesa. Confirma que saliency y objectness son tareas distintas.
2. El afinado con GraspNet lo corrige muy rápido: en la primera época ya encuentra el 98 %.
3. El modelo final encuentra **más del 99 %** de los objetos con un IoU de 0.91.

Observación: `obj_recall` alcanzó su máximo hacia la época 12–15 y luego bajó ligeramente
mientras el IoU seguía subiendo. `best.pth` se guarda según el IoU, así que el modelo
guardado no es el de mayor recall, aunque la diferencia es pequeña (99.3 % frente a 99.6 %).
Si encontrar todos los objetos es lo prioritario, convendría guardar el mejor modelo según
`obj_recall`.

---

## 7. Mapa de archivos

| Archivo | Para qué sirve |
|---|---|
| `src/intel_realsense/intel_realsense/u2net.py` | Definición de la red U²-Net |
| `src/intel_realsense/intel_realsense/u2net_train.py` | Entrenamiento, validación, checkpoints |
| `src/intel_realsense/intel_realsense/u2net_bgrm.py` | Nodo ROS 2 de inferencia en tiempo real |
| `src/intel_realsense/setup.py` | Registro del ejecutable `u2net_bgrm` |
| `scripts/graspnet_export.py` | Convierte GraspNet a pares imagen/máscara |
| `datasets/DUTS-TR/` | Datos de la etapa 1 (saliency) |
| `datasets/GraspNet-1Billion/` | Datos originales de la etapa 2 |
| `datasets/graspnet-obj/` | GraspNet ya convertido (generado por el script) |
| `runs/u2net/v1/`, `runs/u2net/v2/` | Modelos de saliency |
| `runs/u2net/vft1/` | Modelo afinado para objectness |

Cada carpeta de `runs/` contiene:

- `best.pth`: el mejor modelo según el IoU de validación (solo pesos).
- `last.pth`: el estado completo de la última época (para reanudar).
- `metrics.csv`: las métricas de cada época, listas para graficar.

---

## 8. Pasos siguientes sugeridos

1. **Probar `vft1/best.pth` en el robot** con varios objetos sobre la mesa real y compararlo
   con `v2/best.pth` en las mismas escenas.
2. **Guardar el mejor modelo según `obj_recall`** además de según IoU.
3. **Datos propios**: GraspNet usa una mesa y una iluminación concretas. Si la mesa del
   laboratorio es muy distinta, 200–300 imágenes propias etiquetadas cerrarían esa brecha.
4. **Más datos de GraspNet**: reexportar con `--stride 4` duplica las imágenes si se quiere
   exprimir más el dataset.
5. **Separar objetos que se tocan**: hoy dos objetos pegados salen como uno solo. Usar la
   profundidad o una salida extra de bordes ayudaría.
