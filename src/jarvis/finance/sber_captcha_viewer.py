"""Local active-challenge PNG viewer; no plaintext file or network handoff."""
from __future__ import annotations

import base64
import struct
import zlib
from collections.abc import Callable
from pathlib import Path

from jarvis.finance.sber_windows_compat import active_captcha, cancel_inbox
from jarvis.finance.windows_session_store import SessionStorageUnavailable


def ask_fields(fields: list[str], *, image_bytes: bytes | None = None,
               active: Callable[[], None] | None = None, problem: str = "") -> list[str]:
    """Collect credentials/challenges in native Tk; keep values in memory only."""
    import tkinter as tk
    labels = {"login": "Логин", "password": "Пароль", "captcha_code": "Ответ CAPTCHA",
              "otp": "Код SMS", "pin": "PIN", "new_pin": "Новый PIN", "new_pin_confirm": "Повтор PIN"}
    if not fields or len(fields) > 2 or any(f not in labels for f in fields):
        raise SessionStorageUnavailable("Unsupported operator fields")
    if active:
        active()
    if image_bytes is not None:
        validate_png(image_bytes)
    parent = getattr(tk, "_default_root", None)
    try:
        window = tk.Toplevel(parent) if parent is not None else tk.Tk()
    except tk.TclError:
        raise SessionStorageUnavailable("Local operator input unavailable") from None
    window.title("Jarvis — подключение Сбера")
    values: list[str] = []
    entries = []
    timer: str | None = None
    def close() -> None:
        nonlocal timer
        if timer is not None:
            window.after_cancel(timer)
            timer = None
        window.destroy()
    try:
        if problem:
            tk.Label(window, text="Код отклонён. Проверьте ответ и повторите попытку.").pack(padx=16, pady=8)
        if image_bytes is not None:
            image = tk.PhotoImage(master=window, data=base64.b64encode(image_bytes).decode("ascii"))
            tk.Label(window, image=image).pack(padx=16, pady=8)
        for field in fields:
            tk.Label(window, text=labels[field]).pack(padx=16, pady=4)
            entry = tk.Entry(window, show="" if field == "login" else "*")
            entry.pack(padx=16, pady=4)
            entries.append(entry)
        def submit() -> None:
            try:
                if active:
                    active()
            except SessionStorageUnavailable:
                close()
                return
            result = [e.get() for e in entries]
            if not all(result) or any(len(v) > (1024 if f == "password" else 256) for f, v in zip(fields, result, strict=True)):
                return
            if "new_pin_confirm" in fields and result[0] != result[1]:
                return
            values.extend(result)
            for entry in entries:
                entry.delete(0, tk.END)
            close()
        def heartbeat() -> None:
            nonlocal timer
            try:
                if active:
                    active()
            except SessionStorageUnavailable:
                close()
                return
            timer = window.after(500, heartbeat)
        tk.Button(window, text="Продолжить", command=submit).pack(pady=8)
        tk.Button(window, text="Отменить", command=close).pack(pady=8)
        window.protocol("WM_DELETE_WINDOW", close)
        if active:
            timer = window.after(500, heartbeat)
        entries[0].focus_set()
        if parent is not None:
            window.wait_window()
        else:
            window.mainloop()
        if not values:
            raise SessionStorageUnavailable("Operator input cancelled or expired")
        return values
    except tk.TclError:
        raise SessionStorageUnavailable("Local operator input unavailable") from None
    finally:
        image_bytes = None
        try:
            close()
        except tk.TclError:
            pass


def validate_png(raw: bytes) -> tuple[int, int]:
    """Bound dimensions/chunks and reject corrupt or unsupported image artifacts."""
    try:
        if not 33 <= len(raw) <= 1_048_576 or raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError
        offset, count = 8, 0
        width = height = 0
        has_pixels = False
        while offset < len(raw):
            size = int.from_bytes(raw[offset:offset + 4], "big")
            kind = raw[offset + 4:offset + 8]
            end = offset + 12 + size
            if end > len(raw) or count >= 64:
                raise ValueError
            payload = raw[offset + 8:offset + 8 + size]
            crc = int.from_bytes(raw[offset + 8 + size:end], "big")
            if zlib.crc32(kind + payload) != crc:
                raise ValueError
            if count == 0:
                if kind != b"IHDR" or size != 13:
                    raise ValueError
                width, height, depth, color, compression, filtering, interlace = struct.unpack(
                    ">IIBBBBB", payload)
                if (not 1 <= width <= 2048 or not 1 <= height <= 2048 or width * height > 2_000_000
                        or depth != 8 or color not in {0, 2, 3, 4, 6} or compression or filtering or interlace):
                    raise ValueError
            elif kind == b"IHDR":
                raise ValueError
            if kind == b"IDAT":
                has_pixels = has_pixels or bool(size)
            if kind == b"IEND":
                if size or end != len(raw) or not has_pixels:
                    raise ValueError
                return width, height
            offset, count = end, count + 1
        raise ValueError
    except (ValueError, struct.error):
        raise SessionStorageUnavailable("Captcha image unavailable") from None


def show_captcha(ticket: Path, artifact: Path) -> None:
    """Show an encrypted active image, hide answer entry and cancel on close/expiry."""
    import tkinter as tk

    challenge, raw, _ = active_captcha(ticket, artifact)
    validate_png(raw)
    try:
        root = tk.Tk()
    except tk.TclError:
        raise SessionStorageUnavailable("Local captcha viewer unavailable") from None
    root.title("Jarvis: captcha " + challenge)
    root.resizable(False, False)
    try:
        image = tk.PhotoImage(master=root, data=base64.b64encode(raw).decode("ascii"))
        tk.Label(root, image=image).pack(padx=16, pady=16)
        tk.Label(root, text="Ответ вводится скрыто в терминале после закрытия изображения.").pack(padx=16, pady=8)
        submitted = False

        def continue_in_terminal() -> None:
            nonlocal submitted
            # Recheck before handing control to hidden terminal input.
            try:
                active_captcha(ticket, artifact)
            except SessionStorageUnavailable:
                root.destroy()
                return
            submitted = True
            root.destroy()

        def cancel() -> None:
            try:
                cancel_inbox(ticket)
            finally:
                root.destroy()

        def heartbeat() -> None:
            try:
                active_captcha(ticket, artifact)
            except SessionStorageUnavailable:
                root.destroy()
                return
            root.after(500, heartbeat)

        tk.Button(root, text="Ввести ответ в терминале", command=continue_in_terminal).pack(pady=8)
        tk.Button(root, text="Отменить", command=cancel).pack(pady=8)
        root.protocol("WM_DELETE_WINDOW", cancel)
        root.after(500, heartbeat)
        root.mainloop()
        if not submitted:
            raise SessionStorageUnavailable("Captcha cancelled or expired")
    except tk.TclError:
        raise SessionStorageUnavailable("Local captcha viewer unavailable") from None
    finally:
        raw = b""
        try:
            root.destroy()
        except tk.TclError:
            pass


def confirm_operator_read(notice: dict[str, object]) -> bool:
    """Ask the owner locally; never accept a command-line or persistent approval flag."""
    from tkinter import messagebox
    mode = "СИНТЕТИЧЕСКИЙ ТЕСТ, БЕЗ БАНКОВСКОЙ СЕТИ" if notice["mode"] == "TEST_READ" else "Сбер: только чтение"
    selected = notice.get("resource")
    message = (mode + "\nСборка: " + str(notice["artifact_identity"]) + "\nПрофиль: " + str(notice["profile"])
        + "\nОдна сессия, срок 5 минут.\nПериод: " + str(notice["from_date"]) + " — " + str(notice["to_date"])
        + "\nАвторизация: " + str(notice["auth_mode"]) + " (CAPTCHA/SMS/PIN вводятся локально)."
        + ("\nВыбранный ресурс: " + str(selected) + ". Одна страница, максимум 5 операций." if selected else
           "\nСначала вход и список счетов/карт с остатками. Операции только после отдельного выбора ресурса.")
        + "\nПереводы, платежи, изменения продуктов и импорт денег запрещены. Разрешить?")
    return messagebox.askyesno("Одноразовое разрешение чтения", message)


def choose_operator_resource(resources: list[dict[str, object]]) -> str | None:
    """Require an explicit local selection; do not default to an account automatically."""
    import tkinter as tk
    parent = getattr(tk, "_default_root", None)
    window = tk.Toplevel(parent) if parent is not None else tk.Tk()
    window.title("Jarvis — выберите счёт или карту")
    value = tk.StringVar(master=window, value="")
    result: list[str] = []
    tk.Label(window, text="Выберите один ресурс для одной страницы (до пяти операций):").pack(padx=16, pady=8)
    for item in resources:
        resource = str(item["operation_resource"])
        label = resource + " — " + str(item.get("name", ""))[:96]
        tk.Radiobutton(window, text=label, value=resource, variable=value).pack(anchor="w", padx=16)
    def submit() -> None:
        if value.get():
            result.append(value.get())
            window.destroy()
    tk.Button(window, text="Продолжить", command=submit).pack(pady=8)
    tk.Button(window, text="Отменить", command=window.destroy).pack(pady=8)
    if parent is not None:
        window.wait_window()
    else:
        window.mainloop()
    return result[0] if result else None


def operator_window(runtime: Path, *, artifact_identity: str, human: bool = False) -> None:
    """Open the existing native DEMO operator, with explicit bounded read consent."""
    import json
    import tkinter as tk
    from tkinter import messagebox

    from jarvis.finance.sber_launcher import (
        interactive_demo,
        local_profile_status,
        profile_name,
        state_home,
    )
    from jarvis.finance.sber_windows_compat import disconnect_operator

    window = tk.Tk()
    window.title("Jarvis — Сбер: HUMAN READ" if human else "Jarvis — Сбер: DEMO / STATUS")
    status = tk.StringVar(value="Требуется подтверждение. Подключение не выполнено." if human else "DEMO: настоящая банковская сеть отключена")
    tk.Label(window, textvariable=status, wraplength=600).pack(padx=16, pady=8)
    output = tk.Text(window, width=80, height=16)
    output.pack(padx=16, pady=8)
    def refresh() -> None:
        labels = {"saved": "Профиль сохранён; банк ещё не проверен", "disconnected": "Профиль не подключён",
                  "expired": "Сессия истекла: отключите профиль и войдите вручную", "unavailable": "Профиль повреждён или недоступен"}
        status.set(("Требуется подтверждение. " if human else "DEMO. ") + labels[local_profile_status(runtime)])
    def connect(auth_mode: str | None = None) -> None:
        if human:
            try:
                from jarvis.finance.sber_launcher import interactive_operator_read
                status.set("Подключение только для чтения. Введите данные локально.")
                window.update_idletasks()
                report = interactive_operator_read(runtime, ask=ask_fields, confirm=confirm_operator_read, choose=choose_operator_resource, auth_mode=auth_mode)
                output.delete("1.0", tk.END)
                output.insert(tk.END, json.dumps(report, ensure_ascii=False, indent=2))
                status.set(("TEST: банк не подключён. " if report["mode"] == "TEST_READ" else "") + "Получены наблюдения (pending). Сессия завершена.")
            except Exception:  # noqa: BLE001 - never expose provider credentials/diagnostics
                status.set("Источник недоступен, отмена или срок истёк. Повторный вход требуется вручную.")
            return
        # Consent applies to this click/build/profile/bounded DEMO process only.
        notice = ("DEMO: только синтетические данные. Сборка " + artifact_identity[:12]
                  + "; профиль " + profile_name(runtime) + ". До пяти операций за семь дней.\n"
                  + "Переводы, платежи и изменения продуктов недоступны. Выполнить чтение?")
        if not messagebox.askyesno("Одна ограниченная read-сессия", notice, parent=window):
            return
        try:
            report = interactive_demo(runtime, ask_fields)
            from jarvis.finance.review_store import ReviewStore
            from jarvis.finance.review_workbench import workbench_overview
            view = workbench_overview(ReviewStore(Path(report["review_workspace"])))
            shown = {"mode": "DEMO", "bank_access": False, "coverage": report["coverage"],
                     "observed_rub_accounts": report["observed_rub_accounts"], "operations_pending": report["operations"],
                     "observations": view["observed_banking"], "review_workspace": report["review_workspace"]}
            output.delete("1.0", tk.END)
            output.insert(tk.END, json.dumps(shown, ensure_ascii=False, indent=2))
            status.set("DEMO: read завершён, все наблюдения pending. Банковские данные не приняты.")
        except Exception:  # noqa: BLE001 - UI boundary must not render private provider diagnostics
            # Fixed local error only; never show provider repr/tracebacks or secrets.
            status.set("Вход или чтение не выполнены. Отмена, истечение challenge или отклонённый ответ. Можно повторить вручную.")
    def disconnect() -> None:
        if not messagebox.askyesno("Отключить профиль", "Удалить только файлы выбранного собственного профиля?", parent=window):
            return
        try:
            disconnect_operator(state_home(runtime), configuration_id=profile_name(runtime), artifact_identity=artifact_identity)
            refresh()
        except (OSError, ValueError):
            status.set("Отключение недоступно: чужое или повреждённое состояние оставлено без изменения.")
    tk.Button(window, text="Разрешить одну read-сессию" if human else "Подключить / восстановить DEMO", command=connect).pack(pady=4)
    if human:
        tk.Button(window, text="Новая read-сессия: войти PIN вручную", command=lambda: connect("pin")).pack(pady=4)
    tk.Button(window, text="STATUS", command=refresh).pack(pady=4)
    tk.Button(window, text="Отключить свой профиль", command=disconnect).pack(pady=4)
    if not human:
        tk.Label(window, text="DEMO login: synthetic-login; password: synthetic-password; CAPTCHA ABCD; SMS 123456; PIN 31415.").pack(pady=8)
    refresh()
    try:
        window.mainloop()
    finally:
        try:
            window.destroy()
        except tk.TclError:
            pass
