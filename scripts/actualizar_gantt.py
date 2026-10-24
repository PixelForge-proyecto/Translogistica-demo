#!/usr/bin/env python3
"""Sincroniza 57 actividades de GitHub Projects con las fechas del Roadmap."""
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

def read_tasks(items):
    tasks = {}
    for item in items:
        vals = {}
        for field_value in item["fieldValues"]["nodes"]:
            if not field_value or not field_value.get("field"):
                continue
            name = norm(field_value["field"]["name"].strip())
            vals[name] = field_value.get("text", field_value.get("number", field_value.get("date")))
        title = ((item.get("content") or {}).get("title") or "").strip()
        tid = str(vals.get("id") or "").strip().upper()
        raw_duration = str(vals.get("dias") or "").strip()
        if not tid and not raw_duration:
            continue  # Fila PRIMERA ENTREGA: encabezado, no actividad
        match = re.search(r"\d+", raw_duration)
        if not match or int(match.group()) < 1:
            raise ValueError("Duración inválida en " + (tid or title))
        if tid == "T3" and "definir nomenclatura y control de versiones" in norm(title):
            print("AVISO: ID duplicado T3 en nomenclatura; se interpreta como T53. Corregir en Tabla.")
            tid = "T53"
        if not re.fullmatch(r"T\d+", tid) or tid in tasks:
            raise ValueError("ID inválido o repetido: " + tid)
        predecessors = re.findall(r"\bT\d+\b", str(vals.get("precedentes") or "").upper())
        tasks[tid] = {
            "item_id": item["id"], "title": title, "duration": int(match.group()),
            "deps": predecessors
        }
    if len(tasks) != 57:
        raise ValueError(f"Esperaba 57 actividades, encontré {len(tasks)}; no se modificará nada.")
    missing = {dep for t in tasks.values() for dep in t["deps"] if dep not in tasks}
    if missing:
        raise ValueError("Precedentes desconocidos: " + ", ".join(sorted(missing)))
    return tasks

def next_valid(date, weekdays):
    while weekdays and date.weekday() >= 5:
        date += dt.timedelta(days=1)
    return date

def compute_dates(tasks, initial, weekdays):
    done, active = set(), set()
    def compute(tid):
        if tid in done:
            return
        if tid in active:
            raise ValueError("Dependencia circular en " + tid)
        active.add(tid)
        t = tasks[tid]
        for dep in t["deps"]:
            compute(dep)
        candidate = (
            max(tasks[d]["end"] for d in t["deps"]) + dt.timedelta(days=1)
            if t["deps"] else initial
        )
        begin = next_valid(candidate, weekdays)
        end = begin
        for _ in range(t["duration"] - 1):
            end = next_valid(end + dt.timedelta(days=1), weekdays)
        t["start"], t["end"] = begin, end
        active.remove(tid)
        done.add(tid)
    for task_id in tasks:
        compute(task_id)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start-date", default="2026-08-03")
    p.add_argument("--calendar", choices=("weekdays", "calendar"), default="weekdays")
    p.add_argument("--mode", choices=("preview", "update"), default="preview")
    args = p.parse_args()
    token = os.environ.get("GH_PROJECT_TOKEN")
    if not token:
        raise RuntimeError("Falta GH_PROJECT_TOKEN en los secretos de GitHub Actions.")
    project_id, fields, items = fetch_project(token)
    tasks = read_tasks(items)
    compute_dates(tasks, dt.date.fromisoformat(args.start_date), args.calendar == "weekdays")
    print(f"Actividades: {len(tasks)}. Inicio: {args.start_date}. Días: {args.calendar}.")
    for tid, t in tasks.items():
        print(f"{tid:4} {t['start']} → {t['end']}  ({t['duration']}d)  {t['title']}")
    print("Última finalización:", max(t["end"] for t in tasks.values()))
    if args.mode == "preview":
        print("VISTA PREVIA: no se modificó el proyecto.")
        return
    start_field, end_field = fields.get("inicio gantt"), fields.get("fin gantt")
    if not start_field or not end_field:
        raise RuntimeError("Faltan campos de proyecto tipo Date: Inicio Gantt / Fin Gantt.")
    for n, (tid, task) in enumerate(tasks.items(), 1):
        graphql(token, UPDATE, {
            "project": project_id, "item": task["item_id"],
            "startField": start_field, "endField": end_field,
            "startDate": task["start"].isoformat(),
            "endDate": task["end"].isoformat()
        })
        print(f"Actualizada {n}/57: {tid}")
    print("View 2 actualizado. Abrir Roadmap y seleccionar Inicio Gantt / Fin Gantt.")

if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print("ERROR:", error, file=sys.stderr)
        sys.exit(1)
