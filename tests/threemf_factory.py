"""Build small sliced-plate files for tests, shaped like Bambu Studio's exports."""

import io
import json
import zipfile


def sliced_3mf(objects=("Nunu", "Nunu 2", "Nunu 3"), filaments=(("#9D432CFF", "PLA", 30.0),),
               prediction=3600 * 5, weight=40.0, printer_model="Bambu Lab X1 Carbon", changes=3,
               plates=1, project=True, gcode=True) -> bytes:
    plate_xml = "".join(
        "<plate>"
        f'<metadata key="index" value="{i + 1}"/>'
        f'<metadata key="prediction" value="{prediction}"/>'
        f'<metadata key="weight" value="{weight}"/>'
        '<metadata key="printer_model_id" value="BL-P001"/>'
        '<metadata key="nozzle_diameters" value="0.4"/>'
        + "".join(f'<object name="{o}" skipped="false"/>' for o in objects)
        + "".join(f'<filament color="{c}" type="{t}" used_g="{g}"/>' for c, t, g in filaments)
        + "</plate>"
        for i in range(plates)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", f"<config>{plate_xml}</config>")
        if gcode:
            lines = ["; printer_model = " + printer_model] + ["M620 S0A"] * (changes + 1) + ["M620 S255"]
            z.writestr("Metadata/plate_1.gcode", "\n".join(lines) + "\n")
        if project:
            z.writestr("Metadata/project_settings.config", json.dumps({"printer_model": printer_model}))
    return buf.getvalue()


def project_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/model_settings.config",
                   '<config><object id="1"><metadata key="name" value="Nunu"/></object>'
                   '<plate><metadata key="plater_id" value="1"/>'
                   '<model_instance><metadata key="object_id" value="1"/></model_instance>'
                   '<model_instance><metadata key="object_id" value="1"/></model_instance></plate></config>')
        z.writestr("Metadata/project_settings.config",
                   json.dumps({"printer_settings_id": "Bambu Lab H2C 0.4 nozzle",
                               "filament_colour": ["#FF0000"], "filament_type": ["PLA"]}))
    return buf.getvalue()
