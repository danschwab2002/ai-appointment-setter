"""El juez del alta contra el modelo real de OpenRouter. APAGADA por defecto.

La corre la coordinadora (o Dan), con la clave en el entorno y nunca en la linea de comandos:

    SEGMENT_CHECK_REAL=1 SEGMENT_CHECK_GRABAR=/ruta/a/carpeta uv run pytest tests/test_segment_check_real.py

Usa el v14 real de ATT1 y el segmento ficticio de ejemplo sin sus respuestas del alta. No
afirma que el modelo encuentre algo puntual (eso varia entre corridas): afirma que la salida
es valida, que cada cita que queda aparece en su fuente, y que con las respuestas quitadas
hay algo para preguntar. Con SEGMENT_CHECK_GRABAR guarda las respuestas crudas, que pueden
reemplazar a las simuladas de tests/fixtures/segment_judge/.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bridge import segment_check as sc

pytestmark = pytest.mark.skipif(
    os.environ.get("SEGMENT_CHECK_REAL") != "1" or not os.environ.get("OPENROUTER_API_KEY"),
    reason="prueba real del juez, apagada (SEGMENT_CHECK_REAL=1 y OPENROUTER_API_KEY)",
)

FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1-b7580bc-20261103"


def test_the_real_judge_on_the_example_without_answers(tmp_path: Path) -> None:
    text = (FIXTURES / "segments" / "campana-ejemplo-v1.toml").read_text(encoding="utf-8")
    text = text.split("[[preguntas_del_alta]]")[0]
    instance = tmp_path / "instancia"
    import shutil

    shutil.copytree(ATT1, instance)
    segment = instance / "conocimiento" / "segmentos" / "campana-ejemplo-v1.toml"
    segment.parent.mkdir(parents=True)
    segment.write_text(text, encoding="utf-8")
    judge = sc.openrouter_judge(model=os.environ.get("SEGMENT_CHECK_MODEL", sc.DEFAULT_MODEL))
    record = os.environ.get("SEGMENT_CHECK_GRABAR")
    if record:
        judge = sc._recording(judge, Path(record))

    report = sc.check_files(segment, judge=judge, runs=1)

    assert report.complete, [run.error for run in report.runs]
    assert report.items, "sin las respuestas del alta tiene que haber algo para preguntar"
    for item in report.items:
        assert item.question.strip() and item.field.strip()
