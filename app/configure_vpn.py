"""Import a WireGuard provider file locally without printing its contents."""
import argparse
import configparser
import ipaddress
import os
from pathlib import Path
import tempfile

from app.vpn import validate_config
from app.errors import MediaError


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
        # Some providers export dual-stack addresses. The isolated gateway uses the
        # default IPv4 Docker bridge, so an IPv6 interface prevents startup.
        # Normalize only our private copy; preserve the downloaded original.
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read_string(temporary.read_text())
        address_key = next(key for key in config['Interface'] if key.lower() == 'address')
        addresses = [ipaddress.ip_interface(value.strip()) for value in config['Interface'][address_key].split(',')]
        ipv4 = [str(address) for address in addresses if address.version == 4]
        if not ipv4:
            raise MediaError('vpn_config', 'Bu VPN bağlantısı IPv4 adresi içeren bir WireGuard yapılandırması gerektiriyor.')
        if len(ipv4) != len(addresses):
            config['Interface'][address_key] = ', '.join(ipv4)
            with temporary.open('w') as stream:
                config.write(stream)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='WireGuard yapılandırmasını yalnız bu projeye ekler.')
    parser.add_argument('config', type=Path, help='VPN sağlayıcısından indirilen .conf dosyası')
    args = parser.parse_args()
    try:
        import_config(args.config, Path(__file__).resolve().parent.parent / '.data/proton/wg0.conf')
    except (OSError, ValueError):
        parser.exit(1, 'Yapılandırma eklenemedi. Geçerli bir WireGuard dosyası ve dosya izinlerini kontrol et.\n')
    print('WireGuard yapılandırması güvenle kaydedildi. Otomatik VPN sonraki uygun erişim hatasında kullanılacak.')


if __name__ == '__main__':
    main()
