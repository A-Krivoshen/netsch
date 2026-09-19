# netsch

Планировщик сетевых интерфейсов Linux. Открыл программу — меню, как у интегратора: скан, выбор, окна работы, статус, apply, демон, systemd.

**by Aleksey KRIVOSHEIN aka Dr.Slon**  
GitHub: [A-Krivoshen/netsch](https://github.com/A-Krivoshen/netsch) · [krivoshein.site](https://krivoshein.site)

Не набор подкоманд. Cron не используется. Один файл: `netsch.py`.

## Запуск

```bash
python3 netsch.py
```

Нужны Python 3.10+ и `ip` (iproute2). PyYAML желателен (`pip install pyyaml`), без него читается тот же yaml-формат встроенным парсером.

Конфиг: `/etc/netsch/config.yaml` при наличии прав, иначе `./netsch.yaml`.

```yaml
iface:
  eth0:
    enabled: true
    windows:
      - start: "09:00"
        end: "21:00"
check_every_sec: 30
force: false
```

Через полночь можно: `22:00-07:00`.

## Меню

1. Сканировать интерфейсы
2. Выбрать интерфейсы для расписания
3. Задать окна работы
4. Показать статус
5. Применить сейчас
6. Режим демона
7. Установить systemd-сервис
8. Выход

Скан берёт данные только из `ip -br link`, `ip -br addr`, `ip route show default`, `SSH_CONNECTION`. `lo` скрыт.

Curses — если терминал нормальный. Узкий SSH или отсутствие curses → нумерованные экраны через `input()`.

## Логика

- Сейчас внутри окна → интерфейс UP, иначе DOWN.
- Не дёргать, если состояние уже нужное.
- Не трогать `lo`.
- Не гасить интерфейс с default route и интерфейс текущего SSH, пока `force` не true.
- Перед гашением такого интерфейса меню явно спрашивает.

## Логи

`apply` и `run` по умолчанию молчат: нет файла лога, нет syslog, нет записей netsch в journal.

Юнит systemd глушит stdout/stderr (`StandardOutput=null`, `StandardError=null`).

```bash
python3 netsch.py apply          # тихо
python3 netsch.py run            # тихо, цикл
python3 netsch.py apply --dry-run
python3 netsch.py run --verbose  # только если нужно видеть ход в терминале
```

systemd при enable всё равно пишет свои `Started/Stopped netsch.service`. Это сообщения systemd, не netsch.

Ядро/драйвер NIC при `ip link up/down` иногда пишет в dmesg — это не отключаем.

## Сервисные аргументы (автозапуск)

```bash
python3 netsch.py apply
python3 netsch.py apply --dry-run
python3 netsch.py run
python3 netsch.py --config /etc/netsch/config.yaml
python3 netsch.py install
```

`install` пишет `/etc/systemd/system/netsch.service` на `netsch run`. `systemctl enable` сам не выполняется, только показывает команды:

```bash
systemctl daemon-reload
systemctl enable --now netsch.service
```

## Предупреждение: SSH на VPS

Если погасить интерфейс, через который идёт SSH, или интерфейс default route — сессия оборвётся, сервер может стать недоступен.

Сначала `apply --dry-run`. Не ставьте `force: true`, пока не уверены. Держите out-of-band доступ (VNC, IP-KVM, консоль провайдера).

## Лицензия

MIT. © Aleksey KRIVOSHEIN aka Dr.Slon
