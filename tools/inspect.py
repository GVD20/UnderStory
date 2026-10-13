"""Standalone read-only inspector. Never imports the application services."""
import json
import sqlite3
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path

def open_file():
    selected = filedialog.askopenfilename(filetypes=[('Book data', '*.json *.sqlite3 *.db'), ('All', '*.*')])
    if not selected: return
    content.delete('1.0', 'end')
    try:
        source = Path(selected)
        if source.suffix == '.json':
            value = json.dumps(json.loads(source.read_text(encoding='utf-8')), ensure_ascii=False, indent=2)
        else:
            with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as db:
                db.execute('PRAGMA query_only=ON')
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                value = '\n\n'.join(f'{name}\n' + '\n'.join(str(row) for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" LIMIT 50')) for name in tables)
        content.insert('end', value)
    except Exception as error: content.insert('end', str(error))

if __name__ == '__main__':
    root = tk.Tk()
    root.title('UnderStory Data Inspector')
    root.geometry('900x650')
    ttk.Button(root, text='打开 JSON / SQLite', command=open_file).pack(anchor='w', padx=12, pady=12)
    content = tk.Text(root, wrap='none')
    content.pack(fill='both', expand=True, padx=12, pady=(0,12))
    root.mainloop()
