#!/usr/bin/env python3
"""Calcula y sincroniza fechas de una entrega (E1, E2 o E3) en GitHub Projects."""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

ORG, PROJECT_NUMBER = "PixelForge-proyecto", 6
QUERY = """
query($org:String!, $number:Int!, $cursor:String) {
  organization(login:$org) {
    projectV2(number:$number) {
      id
      fields(first:100) {
        nodes {
          ... on ProjectV2Field { id name }
          ... on ProjectV2SingleSelectField { id name }
          ... on ProjectV2IterationField { id name }
        }
      }
      items(first:100,after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            ... on DraftIssue { title }
            ... on Issue { title }
            ... on PullRequest { title }
          }
          fieldValues(first:50) {
            nodes {
              ... on ProjectV2ItemFieldTextValue {
                text
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldNumberValue {
                number
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldDateValue {
                date
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""
UPDATE = """
mutation($project:ID!, $item:ID!, $startField:ID!, $endField:ID!, $startDate:Date!, $endDate:Date!) {
  start: updateProjectV2ItemFieldValue(input:{
    projectId:$project, itemId:$item, fieldId:$startField, value:{date:$startDate}
  }) { projectV2Item { id } }
  finish: updateProjectV2ItemFieldValue(input:{
    projectId:$project, itemId:$item, fieldId:$endField, value:{date:$endDate}
  }) { projectV2Item { id } }
}
"""

def graphql(token, query, variables):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload, method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "translogistica-gantt"
        }
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                data = json.load(response)
            if data.get("errors"):
                raise RuntimeError("; ".join(e.get("message", "Error GraphQL") for e in data["errors"]))
            return data["data"]
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503) and attempt < 2:
                time.sleep(attempt * 2 + 2)
                continue
            raise RuntimeError("GitHub devolvió HTTP " + str(exc.code)) from None

def fetch_project(token):
    items, cursor, project_id, fields = [], None, None, {}
    while True:
        data = graphql(token, QUERY, {
            "org": ORG, "number": PROJECT_NUMBER, "cursor": cursor
        })
        project = (data.get("organization") or {}).get("projectV2")
        if not project:
            raise RuntimeError("Proyecto no accesible: revisar permisos Projects del token.")
        project_id = project["id"]
        fields = {
            f["name"].strip().casefold(): f["id"]
            for f in project["fields"]["nodes"] if f and f.get("name")
        }
        page = project["items"]
        items.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return project_id, fields, items
        cursor = page["pageInfo"]["endCursor"]

def norm(text):
    text = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(c for c in text if not unicodedata.combining(c))

def read_tasks(items, delivery):
    """Devuelve las actividades de la entrega y fechas externas de entregas anteriores."""
    catalog = {}
    tagged_count = 0
    for item in items:
        vals = {}
        for field_value in item["fieldValues"]["nodes"]:
            if not field_value or not field_value.get("field"):
                continue
            name = norm(field_value["field"]["name"].strip())
            vals[name] = field_value.get(
                "name", field_value.get("text", field_value.get("number", field_value.get("date")))
            )
        row_delivery = str(vals.get("entrega") or "").strip().upper()
        if row_delivery == delivery:
            tagged_count += 1
        title = ((item.get("content") or {}).get("title") or "").strip()
        tid = str(vals.get("id") or "").strip().upper()
        raw_duration = str(vals.get("dias") or "").strip()
        if not tid and not raw_duration:
            continue  # Encabezado de entrega, sin tarea
        if not re.fullmatch(r"T\d+", tid) or tid in catalog:
            raise ValueError("ID de actividad inválido o duplicado: " + (tid or title))
        catalog[tid] = {
            "item_id": item["id"], "title": title, "raw_duration": raw_duration,
            "delivery": row_delivery,
            "deps": re.findall(r"\bT\d+\b", str(vals.get("precedentes") or "").upper()),
            "old_start": vals.get("inicio gantt"), "old_end": vals.get("fin gantt")
        }

    tasks = {}
    for tid, row in catalog.items():
        if row["delivery"] != delivery:
            continue
        match = re.fullmatch(
            r"\s*(\d+)\s*(?:D[IÍ]AS?|D)?\s*", row["raw_duration"], re.IGNORECASE
        )
        if not match or int(match.group(1)) < 1:
            raise ValueError("Duración inválida en " + tid + ": " + row["raw_duration"])
        tasks[tid] = {
            **row, "duration": int(match.group(1))
        }

    if not tasks:
        raise ValueError("No hay actividades con Entrega=" + delivery + ". Nada fue modificado.")
    if delivery == "E1" and len(tasks) != 57:
        raise ValueError(
            f"E1 debería tener 57 actividades y encontré {len(tasks)}. "
            "Revisar el campo Entrega antes de actualizar."
        )

    external_ends = {}
    for tid, row in tasks.items():
        for dep in row["deps"]:
            if dep in tasks:
                continue
            prev = catalog.get(dep)
            if not prev:
                raise ValueError(f"{tid}: precedente {dep} inexistente.")
            if prev["delivery"] not in ("E1", "E2", "E3"):
                raise ValueError(f"{tid}: {dep} no tiene entrega definida.")
            if int(prev["delivery"][1]) >= int(delivery[1]):
                raise ValueError(f"{tid}: {dep} pertenece a una entrega posterior o igual.")
            if not prev["old_end"]:
                raise ValueError(
                    f"{tid}: la actividad anterior {dep} no tiene Fin Gantt. "
                    "Calculá primero las entregas anteriores."
                )
            try:
                external_ends[dep] = dt.date.fromisoformat(prev["old_end"])
            except ValueError:
                raise ValueError(f"{tid}: fecha de finalización inválida en {dep}.") from None

    print(
        f"Entrega {delivery}: {len(tasks)} actividades; "
        f"{len(external_ends)} precedentes de entregas anteriores."
    )
    return tasks, external_ends

def next_valid(date, weekdays):
    while weekdays and date.weekday() >= 5:
        date += dt.timedelta(days=1)
    return date

def compute_dates(tasks, external_ends, initial, weekdays):
    done, active = set(), set()

    def compute(tid):
        if tid in done:
            return
        if tid in active:
            raise ValueError("Dependencia circular en " + tid)
        active.add(tid)
        task = tasks[tid]
        predecessor_ends = []
        for dep in task["deps"]:
            if dep in tasks:
                compute(dep)
                predecessor_ends.append(tasks[dep]["end"])
            else:
                predecessor_ends.append(external_ends[dep])
        candidate = max(
            [initial] + [end + dt.timedelta(days=1) for end in predecessor_ends]
        )
        begin = next_valid(candidate, weekdays)
        end = begin
        for _ in range(task["duration"] - 1):
            end = next_valid(end + dt.timedelta(days=1), weekdays)
        task["start"], task["end"] = begin, end
        active.remove(tid)
        done.add(tid)

    for task_id in tasks:
        compute(task_id)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start-date", default="2026-08-03")
    p.add_argument("--delivery", choices=("E1", "E2", "E3"), default="E1")
    p.add_argument("--calendar", choices=("weekdays", "calendar"), default="weekdays")
    p.add_argument("--mode", choices=("preview", "update"), default="preview")
    args = p.parse_args()
    token = os.environ.get("GH_PROJECT_TOKEN")
    if not token:
        raise RuntimeError("Falta GH_PROJECT_TOKEN en los secretos de GitHub Actions.")
    if args.delivery != "E1" and args.start_date == "2026-08-03":
        raise ValueError(
            "Para E2 o E3 elegí una fecha inicial específica; 2026-08-03 es la fecha predeterminada de E1."
        )
    project_id, fields, items = fetch_project(token)
    tasks, external_ends = read_tasks(items, args.delivery)
    compute_dates(tasks, external_ends, dt.date.fromisoformat(args.start_date), args.calendar == "weekdays")
    print(f"Entrega: {args.delivery}. Actividades: {len(tasks)}. "
          f"Inicio: {args.start_date}. Días: {args.calendar}.")
    for tid, t in tasks.items():
        print(f"{tid:4} {t['start']} → {t['end']}  ({t['duration']}d)  {t['title']}")
    print("Última finalización:", max(t["end"] for t in tasks.values()))
    if args.mode == "preview":
        print("VISTA PREVIA: no se modificó el proyecto.")
        return
    start_field, end_field = fields.get("inicio gantt"), fields.get("fin gantt")
    if not start_field or not end_field:
        raise RuntimeError("Faltan campos de proyecto tipo Date: Inicio Gantt / Fin Gantt.")
    changes = [
        (tid, task) for tid, task in tasks.items()
        if task["old_start"] != task["start"].isoformat()
        or task["old_end"] != task["end"].isoformat()
    ]
    print(f"Se actualizarán {len(changes)} de {len(tasks)} actividades de {args.delivery}.")
    for n, (tid, task) in enumerate(changes, 1):
        graphql(token, UPDATE, {
            "project": project_id, "item": task["item_id"],
            "startField": start_field, "endField": end_field,
            "startDate": task["start"].isoformat(),
            "endDate": task["end"].isoformat()
        })
        print(f"Actualizada {n}/{len(changes)}: {tid}")
    print(f"Entrega {args.delivery} sincronizada. Abrí la vista Diagrama en GitHub Projects.")

if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print("ERROR:", error, file=sys.stderr)
        sys.exit(1)
