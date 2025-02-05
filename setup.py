# setup.py
from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="esotericai",
    version="0.0.4", 
    author="esoteric-ai",
    author_email="kiselev.sereja@gmail.com",
    description="Efficient distributed inference.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/esoteric-ai/esoteric-ai",
    packages=find_packages(),
    install_requires=[
        "httpx",
        "websockets",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.7", 
)