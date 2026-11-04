from pif.cli import app

# Пул процессов на Windows импортирует этот модуль заново, поэтому нужна проверка.
if __name__ == "__main__":
    app()
