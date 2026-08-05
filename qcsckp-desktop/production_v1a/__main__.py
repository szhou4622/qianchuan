from .service_main import start_service


def main() -> None:
    service = start_service()
    print(f"QCSCKP V1A service: {service.base_url}")
    try:
        service.thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()


if __name__ == "__main__":
    main()
