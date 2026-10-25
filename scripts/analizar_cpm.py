#!/usr/bin/env python3
"""Calcula holgura CPM y caminos críticos por entrega sin alterar el Gantt.

Holgura: margen frente a la finalización calculada de cada entrega. El margen
hasta la fecha límite de entrega se informa por separado.
"""
import argparse
import datetime as dt
import os
import sys

from actualizar_gantt import compute_dates, fetch_project, graphql, norm, read_tasks

INICIOS = {"E1": "2026-08-03", "E2": "2026-09-28", "E3": "2026-10-29"}
LIMITES = {"E1": "2026-09-29", "E2": "2026-10-28", "E3": "2026-11-28"}

FIELD_QUERY = """
query($org:String!, $number:Int!) {
  organization(login:$org) {
    projectV2(number:$number) {
      fields(first:100) { nodes {
        ... on ProjectV2Field { id name dataType }
        ... on ProjectV2SingleSelectField { id name options { id name } }
      } }
    }
  }
}
"""
CREATE_SIMPLE = """
mutation($project:ID!, $name:String!, $type:ProjectV2CustomFieldType!) {
  createProjectV2Field(input:{projectId:$project,name:$name,dataType:$type}) {
    projectV2Field { ... on ProjectV2Field { id name dataType } }
  }
}
"""
CREATE_SELECT = """
mutation($project:ID!, $name:String!, $options:[ProjectV2SingleSelectFieldOptionInput!]!) {
  createProjectV2Field(input:{projectId:$project,name:$name,
    dataType:SINGLE_SELECT,singleSelectOptions:$options}) {
    projectV2Field { ... on ProjectV2SingleSelectField {
      id name options { id name }
    } }
  }
}
"""
UPDATE_METRICS = """
mutation($project:ID!, $item:ID!, $slackField:ID!, $criticalField:ID!,
         $pathField:ID!, $days:Float!, $option:String!, $paths:String!) {
  a:updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,
    fieldId:$slackField,value:{number:$days}}) { projectV2Item { id } }
  b:updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,
    fieldId:$criticalField,value:{singleSelectOptionId:$option}}) { projectV2Item { id } }
  c:updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,
    fieldId:$pathField,value:{text:$paths}}) { projectV2Item { id } }
}
"""

def calculate(tasks, delivery):
    """CPM hacia atrás desde las fechas tempranas del Gantt."""
    successors = {tid: [] for tid in tasks}
    deps = {tid: [d for d in t["deps"] if d in tasks] for tid, t in tasks.items()}
    for tid, parents in deps.items():
        for p in parents:
            successors[p].append(tid)

    order, visiting, done = [], set(), set()
    def visit(tid):
        if tid in visiting:
            raise ValueError("Dependencia circular en " + tid)
        if tid in done:
            return
        visiting.add(tid)
        for p in deps[tid]:
            visit(p)
        visiting.remove(tid)
        done.add(tid)
        order.append(tid)
    for tid in tasks:
        visit(tid)

    completion = max(t["end"] for t in tasks.values())
    latest_start, latest_end, slack = {}, {}, {}
    for tid in reversed(order):
        task = tasks[tid]
        latest_end[tid] = min(
            (latest_start[s] - dt.timedelta(days=1) for s in successors[tid]),
            default=completion,
        )
        latest_start[tid] = latest_end[tid] - dt.timedelta(days=task["duration"] - 1)
        slack[tid] = (latest_start[tid] - task["start"]).days
        if slack[tid] < 0:
            raise ValueError(tid + ": holgura negativa. Revisar programación y duración")

    critical = {tid for tid, value in slack.items() if value == 0}
    paths = []
    def trace_back(tid, tail):
        previous = [p for p in deps[tid]
                    if p in critical and
                    tasks[p]["end"] + dt.timedelta(days=1) == tasks[tid]["start"]]
        if not previous:
            paths.append(list(reversed(tail + [tid])))
            return
        for p in previous:
            trace_back(p, tail + [tid])

    for tid in order:
        if tid in critical and not successors[tid] and tasks[tid]["end"] == completion:
            trace_back(tid, [])
    paths.sort(key=lambda ids: [int(i[1:]) for i in ids])
    if {tid for path in paths for tid in path} != critical:
        raise ValueError(delivery + ": no se reconstruyeron todos los caminos críticos")
    membership = {tid: [] for tid in tasks}
    for number, path in enumerate(paths, 1):
        for tid in path:
            membership[tid].append(f"{delivery}-C{number}")

    deadline = dt.date.fromisoformat(LIMITES[delivery])
    buffer = (deadline - completion).days
    if buffer < 0:
        raise ValueError(f"{delivery}: se excede la fecha límite {deadline} en {-buffer} días")
    return {"slack": slack, "critical": critical, "paths": paths,
            "membership": membership, "finish": completion, "buffer": buffer}

def list_project_fields(token):
    data = graphql(token, FIELD_QUERY, {"org": "PixelForge-proyecto", "number": 6})
    project = (data.get("organization") or {}).get("projectV2")
    if not project:
        raise RuntimeError("No se pudo acceder a los campos del proyecto")
    return {norm(f["name"]): f for f in project["fields"]["nodes"]
            if f and f.get("name")}

def ensure_fields(token, project_id):
    """Crea campos nuevos solamente si no existen; no cambia los existentes."""
    fields = list_project_fields(token)
    for name, dtype in [("Holgura", "NUMBER"), ("Camino crítico", "TEXT")]:
        if norm(name) not in fields:
            print(f"Creando columna {name} ({dtype})...")
            graphql(token, CREATE_SIMPLE,
                    {"project": project_id, "name": name, "type": dtype})
    if norm("Actividad crítica") not in fields:
        print("Creando columna Actividad crítica (Sí/No)...")
        graphql(token, CREATE_SELECT, {"project": project_id,
            "name": "Actividad crítica",
            "options": [
                {"name": "Sí", "color": "RED",
                 "description": "Pertenece a un camino crítico de su entrega"},
                {"name": "No", "color": "GRAY",
                 "description": "Tiene holgura positiva"},
            ]})
    fields = list_project_fields(token)
    for name, dtype in [("Holgura", "NUMBER"), ("Camino crítico", "TEXT")]:
        f = fields.get(norm(name))
        if not f or f.get("dataType") != dtype:
            raise ValueError(f"La columna {name} debe ser de tipo {dtype}")
    critical_field = fields.get(norm("Actividad crítica"))
    if not critical_field or "options" not in critical_field:
        raise ValueError("Actividad crítica debe ser selección única (Sí/No)")
    options = {norm(o["name"]): o["id"] for o in critical_field["options"]}
    if not all(key in options for key in ("si", "no")):
        raise ValueError("Actividad crítica debe contener las opciones Sí y No")
    return (fields[norm("Holgura")]["id"], critical_field["id"],
            fields[norm("Camino crítico")]["id"], options)

def current_metrics(items):
    found = {}
    for item in items:
        fields = {}
        for v in item["fieldValues"]["nodes"]:
            if not v or not v.get("field"):
                continue
            n = norm(v["field"]["name"])
            fields[n] = v.get("name", v.get("text", v.get("number")))
        tid = str(fields.get("id") or "").strip().upper()
        if tid:
            found[tid] = fields
    return found

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delivery", choices=("ALL", "E1", "E2", "E3"), default="ALL")
    parser.add_argument("--mode", choices=("preview", "update"), default="preview")
    args = parser.parse_args()
    token = os.environ.get("GH_PROJECT_TOKEN")
    if not token:
        raise RuntimeError("Falta GH_PROJECT_TOKEN (secreto GANTT_PROJECT_TOKEN)")
    project_id, _, items = fetch_project(token)
    deliveries = ["E1", "E2", "E3"] if args.delivery == "ALL" else [args.delivery]
    analysis = {}

    for delivery in deliveries:
        tasks, external_ends = read_tasks(items, delivery)
        compute_dates(tasks, external_ends, dt.date.fromisoformat(INICIOS[delivery]), False)
        for tid, task in tasks.items():
            if (task["old_start"] != task["start"].isoformat()
                    or task["old_end"] != task["end"].isoformat()):
                raise ValueError(
                    f"{tid}: GitHub no coincide con las fechas calculadas. "
                    "Ejecutá primero el workflow Actualizar Gantt para esta entrega.")
        result = calculate(tasks, delivery)
        analysis[delivery] = (tasks, result)
        print(f"\n== {delivery}: {len(tasks)} tareas, "
              f"{len(result['critical'])} críticas, {len(result['paths'])} caminos; "
              f"fin {result['finish']}; margen hasta {LIMITES[delivery]}: "
              f"{result['buffer']} días ==")
        for i, path in enumerate(result["paths"], 1):
            print(f"{delivery}-C{i}: " + " → ".join(path))
        for tid in sorted(tasks, key=lambda value: int(value[1:])):
            labels = ", ".join(result["membership"][tid]) or "—"
            print(f"{tid:5} holgura={result['slack'][tid]:2d}d "
                  f"crítica={'Sí' if tid in result['critical'] else 'No':2} "
                  f"camino={labels}")

    if args.mode == "preview":
        print("\nVISTA PREVIA: no se modificó el proyecto.")
        return

    slack_field, critical_field, path_field, options = ensure_fields(token, project_id)
    existing = current_metrics(items)
    changes = 0
    for delivery in deliveries:
        tasks, data = analysis[delivery]
        for tid in sorted(tasks, key=lambda value: int(value[1:])):
            days = data["slack"][tid]
            label = ", ".join(data["membership"][tid]) or "—"
            status = "Sí" if tid in data["critical"] else "No"
            previous = existing.get(tid, {})
            if (previous.get(norm("Holgura")) == days
                    and previous.get(norm("Actividad crítica")) == status
                    and previous.get(norm("Camino crítico")) == label):
                continue
            graphql(token, UPDATE_METRICS,
                    {"project": project_id, "item": tasks[tid]["item_id"],
                     "slackField": slack_field, "criticalField": critical_field,
                     "pathField": path_field, "days": float(days),
                     "option": options[norm(status)], "paths": label})
            changes += 1
            print("Actualizada holgura/criticidad:", tid)
    print(f"\nActualizadas {changes} actividades. "
          "Inicio Gantt y Fin Gantt no fueron modificados.")

if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print("ERROR:", error, file=sys.stderr)
        sys.exit(1)
