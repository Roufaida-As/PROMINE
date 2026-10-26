import csv
import sys
from pathlib import Path


csv_path = Path(__file__).with_name("prompt_commits.csv")
ROW_NUMBER = 1533


def set_max_csv_field_limit():
	max_int = sys.maxsize
	while True:
		try:
			csv.field_size_limit(max_int)
			break
		except OverflowError:
			max_int //= 10


def get_csv_stats(csv_path):
	with csv_path.open("r", encoding="utf-8", newline="") as csv_file:
		reader = csv.DictReader(csv_file)
		rows = list(reader)

	return {
		"total_rows": len(rows),
		"non_empty_prompt_var_names": sum(
			1 for row in rows if row.get("prompt_var_names", "").strip()
		),
		"rows_after_target": max(0, len(rows) - ROW_NUMBER),
	}


set_max_csv_field_limit()

stats = get_csv_stats(csv_path)

print(f"{csv_path.name} has {stats['total_rows']} data rows.")
print(
	f"Rows with non-empty prompt_var_names: "
	f"{stats['non_empty_prompt_var_names']}"
)
print(f"Rows after row {ROW_NUMBER}: {stats['rows_after_target']}")
