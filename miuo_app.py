"""Point d'entrée des exécutables Windows (Miuo.exe et miuo-cli.exe)."""
import sys

from miuo.cli import main

if __name__ == "__main__":
    sys.exit(main())
