"""Include the checked-in dependency notices alongside a portable build."""
import argparse
import shutil
from pathlib import Path


def package(source, destination):
    notices = Path(source).resolve() / 'docs' / 'licenses'
    target = Path(destination).resolve() / 'licenses'
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for file in notices.iterdir():
        if file.is_file() and not file.is_symlink():
            shutil.copy2(file, target / file.name)
            count += 1
    for name in ('LICENSE', 'NOTICE'):
        shutil.copy2(Path(source) / name, Path(destination) / name)
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path('.'))
    parser.add_argument('--destination', type=Path, required=True)
    arguments = parser.parse_args()
    print(package(arguments.source, arguments.destination))
