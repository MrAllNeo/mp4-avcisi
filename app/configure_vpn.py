"""Import a Proton WireGuard file locally without printing its contents."""
import argparse
import os
from pathlib import Path
import tempfile

from app.vpn import validate_config


def import_config(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    # Limit the read too: a wrong file must not consume arbitrary memory.
    with Path(source).open('rb') as stream:
        data = stream.read(16385)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.wireguard-')
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        validate_config(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='Proton WireGuard yapılandırmasını yalnız bu projeye ekler.')
    parser.add_argument('config', type=Path, help='Proton hesabından indirilen .conf dosyası')
    args = parser.parse_args()
    try:
        import_config(args.config, Path(__file__).resolve().parent.parent / '.data/proton/wg0.conf')
    except (OSError, ValueError):
        parser.exit(1, 'Yapılandırma eklenemedi. Geçerli bir Proton WireGuard dosyası ve dosya izinlerini kontrol et.\n')
    print('Proton yapılandırması güvenle kaydedildi. Otomatik VPN sonraki uygun erişim hatasında kullanılacak.')


if __name__ == '__main__':
    main()
