"""Run Nexora Books from source, without packaging it.

    python run.py

Useful while developing, or on a machine where you would rather not build
the .exe. It behaves exactly like NexoraBooks.exe.
"""
from desktop import main

if __name__ == "__main__":
    main()
