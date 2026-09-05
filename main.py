"""CLI-утилита для объединения частей с копированием видео без перекодирования."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

console = Console()

# Сообщения FFmpeg, указывающие на проблемы с таймстампами при склейке.
DTS_WARNING_MARKERS = (
    "Non-monotonic DTS",
    "Non-monotonous DTS",
    "Non-monotonically increasing DTS",
    "Invalid DTS",
)

AAC_RECOVERY_MARKERS = (
    "Error parsing ADTS frame header",
    "Error applying bitstream filters to an output packet for stream",
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


@dataclass
class MergeJob:
    directory: Path
    files: list[Path]

    @property
    def output(self) -> Path:
        return self.directory / "video.mp4"

    @property
    def review_marker(self) -> Path:
        return self.directory / ".video-joiner-needs-review"


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


def build_ffmpeg_command(
    concat_file: Path, output: Path, *, transcode_audio: bool = False
) -> list[str]:
    """Строит команду FFmpeg, исключая несовместимые служебные потоки."""
    command = [
        "ffmpeg", "-y",
    ]
    if transcode_audio:
        command.extend([
            "-fflags", "+discardcorrupt+genpts",
            "-err_detect", "ignore_err",
        ])
    command.extend([
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-map", "0:v",
        "-map", "0:a?",
    ])
    if transcode_audio:
        command.extend(["-c:v", "copy", "-c:a", "aac"])
    else:
        command.extend(["-c", "copy"])
    command.extend(["-progress", "pipe:1", "-nostats", str(output)])
    return command


def execute_ffmpeg(
    command: list[str], total_duration: float, progress_label: str
) -> tuple[int, str, list[str], bool]:
    """Выполняет FFmpeg и возвращает код, хвост лога, DTS-предупреждения и признак AAC-ошибки."""
    logger.debug("Команда FFmpeg: {}", command)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    ffmpeg_output: deque[str] = deque(maxlen=2000)
    warnings_found: set[str] = set()
    aac_recovery_needed = False

    with Progress(
        TextColumn(f"[bold]{progress_label}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        TextColumn("осталось:"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("merge", total=100)
        assert process.stdout is not None
        for raw_line in process.stdout:
            ffmpeg_output.append(raw_line)
            warnings_found.update(
                marker for marker in DTS_WARNING_MARKERS if marker in raw_line
            )
            if any(marker in raw_line for marker in AAC_RECOVERY_MARKERS):
                aac_recovery_needed = True
            line = raw_line.strip()
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

    process.wait()
    combined_output = "".join(ffmpeg_output)
    logger.debug("Вывод FFmpeg:\n{}", combined_output)
    warnings = [marker for marker in DTS_WARNING_MARKERS if marker in warnings_found]
    return process.returncode, combined_output, warnings, aac_recovery_needed


def run_ffmpeg_concat(
    concat_file: Path, output: Path, total_duration: float
) -> tuple[list[str], bool]:
    """Объединяет части, повторяя попытку с перекодированием повреждённого AAC."""
    command = build_ffmpeg_command(concat_file, output)
    returncode, ffmpeg_output, warnings, aac_recovery_needed = execute_ffmpeg(
        command, total_duration, "Объединение видео"
    )

    if returncode == 0:
        return warnings, False

    if aac_recovery_needed:
        console.print(
            "[yellow]Обнаружен повреждённый AAC-пакет. Повторная попытка с "
            "перекодированием только аудио...[/yellow]"
        )
        recovery_command = build_ffmpeg_command(
            concat_file, output, transcode_audio=True
        )
        recovery_code, recovery_output, recovery_warnings, _ = execute_ffmpeg(
            recovery_command, total_duration, "Восстановление аудио"
        )
        warnings = list(dict.fromkeys([*warnings, *recovery_warnings]))
        if recovery_code == 0:
            return warnings, True
        logger.debug(
            "FFmpeg завершился с ошибкой после повторной попытки:\n{}",
            recovery_output,
        )
        raise RuntimeError(
            "FFmpeg не смог объединить файлы даже после восстановления аудио; "
            "исходники сохранены"
        )

    logger.debug("FFmpeg завершился с ошибкой:\n{}", ffmpeg_output)
    raise RuntimeError("FFmpeg завершился с ошибкой, подробности в логе (--verbose)")


def creation_time(path: Path) -> float:
    """Возвращает время создания файла или ctime, если birth time недоступен."""
    stat = path.stat()
    return getattr(stat, "st_birthtime", stat.st_ctime)


def discover_jobs(root: Path) -> list[MergeJob]:
    """Находит во всём дереве каталоги с исходными MP4-файлами."""
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    jobs = []
    for directory in sorted(directories, key=lambda path: str(path).casefold()):
        files = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".mp4"
            and path.name.casefold() != "video.mp4"
        ]
        if files:
            files.sort(key=lambda path: (creation_time(path), path.name.casefold()))
            jobs.append(MergeJob(directory=directory, files=files))
    return jobs


def select_jobs(jobs: list[MergeJob], force: bool) -> tuple[list[MergeJob], list[MergeJob]]:
    """Отделяет задания с готовым video.mp4, если перезапись не запрошена."""
    if force:
        return jobs, []
    pending = [job for job in jobs if not job.output.exists()]
    skipped = [job for job in jobs if job.output.exists()]
    return pending, skipped


def requeue_invalid_outputs(
    pending: list[MergeJob], completed: list[MergeJob]
) -> tuple[list[MergeJob], list[MergeJob], list[tuple[MergeJob, str]]]:
    """Возвращает задания с повреждённым video.mp4 в очередь на пересборку."""
    ready = []
    invalid = []
    pending = list(pending)
    for job in completed:
        try:
            probe_file(job.output)
        except RuntimeError as error:
            pending.append(job)
            invalid.append((job, str(error)))
        else:
            ready.append(job)
    pending.sort(key=lambda job: str(job.directory).casefold())
    return pending, ready, invalid


def delete_sources(files: list[Path]) -> int:
    """Удаляет исходные MP4-файлы после проверки итогового video.mp4."""
    if any(file.name.casefold() == "video.mp4" for file in files):
        raise ValueError("Итоговый video.mp4 не может быть удалён как исходник")
    deleted = 0
    for file in files:
        file.unlink()
        logger.debug("Удалён исходный файл: {}", file)
        deleted += 1
    return deleted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="video-joiner",
        description=(
            "Рекурсивно объединяет MP4-файлы в каждой директории "
            "в локальный video.mp4 без перекодирования видео."
        ),
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="Корневая директория для рекурсивного поиска файлов .mp4",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перезаписать существующие video.mp4",
    )
    parser.add_argument(
        "--keep-sources",
        action="store_true",
        help="Не удалять исходные MP4 после успешного объединения",
    )
    parser.add_argument("--verbose", action="store_true", help="Подробный вывод логов")
    parser.add_argument("--dry-run", action="store_true", help="Только проверка файлов, без объединения")
    return parser.parse_args()


def run() -> None:
    args = parse_args()

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "WARNING")

    check_dependencies()

    if not args.directory.is_dir():
        console.print(f"[red]Ошибка:[/red] директория не найдена: {args.directory}")
        sys.exit(1)

    jobs = discover_jobs(args.directory)
    if not jobs:
        console.print(
            f"[yellow]В директории {args.directory} и её поддиректориях "
            "не найдено исходных файлов .mp4[/yellow]"
        )
        return

    jobs, skipped_jobs = select_jobs(jobs, args.force)
    jobs, skipped_jobs, invalid_outputs = requeue_invalid_outputs(jobs, skipped_jobs)
    if invalid_outputs:
        console.print("[yellow]Повреждённые video.mp4 будут пересозданы:[/yellow]")
        for job, error in invalid_outputs:
            console.print(f"  - {job.output}")
            logger.debug("Причина пересборки {}: {}", job.output, error)
    review_jobs = [job for job in skipped_jobs if job.review_marker.exists()]
    skipped_jobs = [job for job in skipped_jobs if not job.review_marker.exists()]
    if review_jobs:
        console.print(
            "[yellow]Директории с предупреждениями DTS требуют ручной проверки; "
            "исходники сохранены:[/yellow]"
        )
        for job in review_jobs:
            console.print(f"  - {job.directory}")
    if skipped_jobs:
        console.print("[yellow]Найдены директории с готовым video.mp4:[/yellow]")
        for job in skipped_jobs:
            console.print(f"  - {job.directory}")

    if not jobs and args.keep_sources:
        console.print("[green]Все найденные директории уже обработаны.[/green]")
        return

    if jobs:
        console.print(f"[bold]Найдено директорий для объединения: {len(jobs)}[/bold]")
    prepared_jobs: list[tuple[MergeJob, list[MediaInfo], float]] = []
    has_problems = False
    for job in jobs:
        console.print(f"\n[bold cyan]{job.directory}[/bold cyan]")
        infos = [probe_file(file) for file in job.files]
        show_table(infos)
        problems = check_compatibility(infos)
        if problems:
            has_problems = True
            console.print("[red]Несовместимые части:[/red]")
            for problem in problems:
                console.print(f"  - {problem}")
        prepared_jobs.append((job, infos, sum(info.duration for info in infos)))

    if has_problems:
        console.print(
            "\n[red]Объединение отменено.[/red] Приведите перечисленные части "
            "к одинаковым параметрам и повторите попытку."
        )
        sys.exit(1)

    if args.dry_run:
        console.print("\n[yellow]Режим --dry-run: объединение не выполняется[/yellow]")
        for job, _, total_duration in prepared_jobs:
            console.print(
                f"{job.output}: {len(job.files)} файлов, "
                f"ожидаемая длительность {format_duration(total_duration)}"
            )
        if skipped_jobs and not args.keep_sources:
            files_to_delete = sum(len(job.files) for job in skipped_jobs)
            console.print(
                f"После проверки готовых video.mp4 было бы удалено исходников: "
                f"{files_to_delete}"
            )
        return

    started_at = time.monotonic()
    deleted_files = 0
    audio_transcoded_files = 0
    if not args.keep_sources:
        for job in skipped_jobs:
            probe_file(job.output)
            deleted = delete_sources(job.files)
            deleted_files += deleted
            console.print(
                f"[green]Очищена {job.directory}[/green] — удалено исходников: {deleted}"
            )

    for index, (job, _, total_duration) in enumerate(prepared_jobs, start=1):
        console.print(f"\n[bold]Директория {index}/{len(prepared_jobs)}: {job.directory}[/bold]")
        concat_file = build_concat_file(job.files)
        try:
            warnings_found, audio_transcoded = run_ffmpeg_concat(
                concat_file, job.output, total_duration
            )
        finally:
            concat_file.unlink(missing_ok=True)

        if audio_transcoded:
            audio_transcoded_files += 1

        if warnings_found:
            job.review_marker.write_text(
                "FFmpeg сообщил о проблемах с DTS. Проверьте video.mp4 вручную "
                "перед удалением исходников.\n",
                encoding="utf-8",
            )
            review_jobs.append(job)
            console.print(
                f"[yellow]Предупреждение:[/yellow] FFmpeg сообщил о проблемах с таймстампами "
                f"({', '.join(warnings_found)}). Файл {job.output} стоит проверить вручную; "
                "исходники будут сохранены."
            )
        else:
            job.review_marker.unlink(missing_ok=True)

        result_info = probe_file(job.output)
        deleted = 0
        if not args.keep_sources and not warnings_found:
            deleted = delete_sources(job.files)
            deleted_files += deleted
        console.print(
            f"[green]Создан {job.output}[/green] — {format_duration(result_info.duration)}, "
            f"{format_size(result_info.size)}; удалено исходников: {deleted}"
            + ("; аудио восстановлено" if audio_transcoded else "")
        )

    elapsed = time.monotonic() - started_at
    console.print()
    console.print("[bold green]Готово[/bold green]")
    console.print(f"Обработано директорий: {len(prepared_jobs)}")
    console.print(f"С готовым результатом: {len(skipped_jobs)}")
    console.print(f"Требуют проверки:       {len(review_jobs)}")
    console.print(f"Создано файлов:        {len(prepared_jobs)}")
    console.print(f"Удалено исходников:    {deleted_files}")
    console.print(f"С восстановлением аудио: {audio_transcoded_files}")
    console.print("Перекодировка видео:     нет")
    console.print(f"Время работы:            {format_duration(elapsed)}")


def main() -> None:
    try:
        run()
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        console.print(f"[red]Ошибка:[/red] {error}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
