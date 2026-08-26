"""CLI-утилита для объединения нескольких частей видео в один файл без перекодирования."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

console = Console()

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".ts", ".m4v", ".webm"}

# Сообщения FFmpeg, указывающие на проблемы с таймстампами при склейке.
DTS_WARNING_MARKERS = (
    "Non-monotonous DTS",
    "Non-monotonically increasing DTS",
    "Invalid DTS",
)


@dataclass
class MediaInfo:
    path: Path
    video_codec: str
    width: int
    height: int
    fps: float
    audio_codec: str
    sample_rate: int
    channels: int
    duration: float
    size: int


def check_dependencies() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            console.print(f"[red]Ошибка:[/red] не найден '{tool}'. Установите FFmpeg и повторите попытку.")
            sys.exit(1)


def parse_fps(rate: str) -> float:
    """Преобразует значение вида '30000/1001' в число кадров в секунду."""
    if not rate or rate == "0/0":
        return 0.0
    num, _, den = rate.partition("/")
    den = den or "1"
    try:
        return round(float(num) / float(den), 3)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_file(path: Path) -> MediaInfo:
    """Получает метаданные файла через ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    logger.debug("Запуск ffprobe: {}", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe не смог обработать файл {path}: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise RuntimeError(f"В файле {path} не найден видеопоток")

    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)

    info = MediaInfo(
        path=path,
        video_codec=video.get("codec_name", "?"),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        fps=parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate", "0/0")),
        audio_codec=audio.get("codec_name", "-") if audio else "-",
        sample_rate=int(audio.get("sample_rate", 0)) if audio else 0,
        channels=int(audio.get("channels", 0)) if audio else 0,
        duration=duration,
        size=path.stat().st_size,
    )
    logger.debug("Метаданные {}: {}", path.name, info)
    return info


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ПБ"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def show_table(infos: list[MediaInfo]) -> None:
    table = Table(title="Части видео")
    table.add_column("Файл")
    table.add_column("Видео")
    table.add_column("Разрешение")
    table.add_column("FPS")
    table.add_column("Аудио")
    table.add_column("Размер")
    for info in infos:
        table.add_row(
            info.path.name,
            info.video_codec,
            f"{info.width}x{info.height}",
            str(info.fps),
            info.audio_codec,
            format_size(info.size),
        )
    console.print(table)


def check_compatibility(infos: list[MediaInfo]) -> list[str]:
    """Сравнивает части видео и возвращает причины, из-за которых -c copy небезопасен."""
    problems = []
    first = infos[0]
    for info in infos[1:]:
        if info.video_codec != first.video_codec:
            problems.append(
                f"{info.path.name}: другой видеокодек ({info.video_codec} != {first.video_codec})"
            )
        if (info.width, info.height) != (first.width, first.height):
            problems.append(
                f"{info.path.name}: другое разрешение "
                f"({info.width}x{info.height} != {first.width}x{first.height})"
            )
        if abs(info.fps - first.fps) > 0.05:
            problems.append(f"{info.path.name}: другой FPS ({info.fps} != {first.fps})")
        if info.audio_codec != first.audio_codec:
            problems.append(
                f"{info.path.name}: другой аудиокодек ({info.audio_codec} != {first.audio_codec})"
            )
        if info.sample_rate != first.sample_rate:
            problems.append(
                f"{info.path.name}: другая частота дискретизации аудио "
                f"({info.sample_rate} != {first.sample_rate})"
            )
        if info.channels != first.channels:
            problems.append(
                f"{info.path.name}: другое число аудиоканалов ({info.channels} != {first.channels})"
            )
    return problems


def build_concat_file(files: list[Path]) -> Path:
    """Создаёт временный список файлов для FFmpeg concat demuxer."""
    fd, path_str = tempfile.mkstemp(prefix="video_joiner_", suffix=".txt")
    concat_path = Path(path_str)
    with open(fd, "w", encoding="utf-8") as f:
        for file in files:
            # Экранируем одинарные кавычки в пути для формата concat demuxer.
            escaped = str(file.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    return concat_path


def run_ffmpeg_concat(concat_file: Path, output: Path, total_duration: float) -> list[str]:
    """Запускает объединение и показывает прогресс через Rich. Возвращает найденные предупреждения о таймстампах."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-map", "0",
        "-c", "copy",
        "-progress", "pipe:1",
        "-nostats",
        str(output),
    ]
    logger.debug("Команда FFmpeg: {}", cmd)

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    with Progress(
        TextColumn("[bold]Объединение видео"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        TextColumn("осталось:"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("merge", total=100)
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "out_time_ms" and total_duration > 0:
                try:
                    current_seconds = int(value) / 1_000_000
                    percent = min(100.0, current_seconds / total_duration * 100)
                    progress.update(task, completed=percent)
                except ValueError:
                    pass
            elif key == "progress" and value == "end":
                progress.update(task, completed=100)

    stderr_output = process.stderr.read() if process.stderr else ""
    process.wait()
    logger.debug("stderr FFmpeg:\n{}", stderr_output)

    if process.returncode != 0:
        logger.error("FFmpeg завершился с ошибкой:\n{}", stderr_output)
        raise RuntimeError("FFmpeg завершился с ошибкой, подробности в логе (--verbose)")

    return [marker for marker in DTS_WARNING_MARKERS if marker in stderr_output]


def collect_files(files: list[Path]) -> list[Path]:
    """Если передан один аргумент и это директория — берёт из неё все .mp4 в алфавитном порядке."""
    if len(files) == 1 and files[0].is_dir():
        directory = files[0]
        found = sorted(directory.glob("*.mp4"))
        if not found:
            console.print(f"[red]Ошибка:[/red] в директории {directory} не найдено файлов .mp4")
            sys.exit(1)
        return found
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="video-joiner",
        description="Объединяет несколько последовательных частей видео в один файл без перекодирования.",
    )
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="Файлы частей видео в порядке склейки, либо одна директория с файлами .mp4",
    )
    parser.add_argument("-o", "--output", type=Path, required=True, help="Путь к итоговому файлу")
    parser.add_argument("--force", action="store_true", help="Перезаписать output, если он уже существует")
    parser.add_argument("--verbose", action="store_true", help="Подробный вывод логов")
    parser.add_argument("--dry-run", action="store_true", help="Только проверка файлов, без объединения")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "WARNING")

    check_dependencies()

    args.files = collect_files(args.files)

    for file in args.files:
        if not file.exists():
            console.print(f"[red]Ошибка:[/red] файл не найден: {file}")
            sys.exit(1)
        if file.suffix.lower() not in VIDEO_EXTENSIONS:
            logger.warning("Неизвестное расширение файла: {}", file)

    if args.output.exists() and not args.force:
        console.print(
            f"[red]Ошибка:[/red] файл {args.output} уже существует. "
            "Используйте --force для перезаписи."
        )
        sys.exit(1)

    console.print("[bold]Проверка частей видео...[/bold]")
    infos = [probe_file(f) for f in args.files]
    show_table(infos)

    problems = check_compatibility(infos)
    if problems:
        console.print(
            "[red]Части видео несовместимы, объединение через -c copy небезопасно:[/red]"
        )
        for problem in problems:
            console.print(f"  - {problem}")
        console.print(
            "Автоматическая перекодировка не выполняется. "
            "Приведите части к одинаковым параметрам и повторите попытку."
        )
        sys.exit(1)

    total_duration = sum(info.duration for info in infos)

    if args.dry_run:
        console.print("[yellow]Режим --dry-run: объединение не выполняется[/yellow]")
        console.print(f"Файлы совместимы. Ожидаемая длительность: {format_duration(total_duration)}")
        console.print(f"Результат будет сохранён в: {args.output}")
        return

    concat_file = build_concat_file(args.files)
    start = time.monotonic()
    try:
        warnings_found = run_ffmpeg_concat(concat_file, args.output, total_duration)
    finally:
        concat_file.unlink(missing_ok=True)
    elapsed = time.monotonic() - start

    if warnings_found:
        console.print(
            f"[yellow]Предупреждение:[/yellow] FFmpeg сообщил о проблемах с таймстампами "
            f"({', '.join(warnings_found)}). Результат стоит проверить вручную."
        )

    result_info = probe_file(args.output)

    console.print()
    console.print("[bold green]Готово[/bold green]")
    console.print(f"Файлов:        {len(args.files)}")
    console.print(f"Длительность:  {format_duration(result_info.duration)}")
    console.print(f"Размер:        {format_size(result_info.size)}")
    console.print("Перекодировка: нет")
    console.print(f"Результат:     {args.output}")
    console.print(f"Время работы:  {format_duration(elapsed)}")


if __name__ == "__main__":
    main()
