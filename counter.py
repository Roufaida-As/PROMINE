import csv
import sys
from pathlib import Path


csv_path = Path(__file__).with_name("prompt_commits.csv")


def set_max_csv_field_limit():
	max_int = sys.maxsize
	while True:
		try:
			csv.field_size_limit(max_int)
			break
		except OverflowError:
			max_int //= 10


def count_non_empty_prompt_var_names(csv_path):
	with csv_path.open("r", encoding="utf-8", newline="") as csv_file:
		reader = csv.DictReader(csv_file)
		return sum(
			1
			for row in reader
			if row.get("prompt_var_names", "").strip()
		)


set_max_csv_field_limit()

with csv_path.open("r", encoding="utf-8") as csv_file:
	line_count = sum(1 for _ in csv_file)

print(f"{csv_path.name} has {line_count} lines.")
print(
	f"Rows with non-empty prompt_var_names: "
	f"{count_non_empty_prompt_var_names(csv_path)}"
)
