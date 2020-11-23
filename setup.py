from setuptools import setup
import os


# read the contents of your README file
path = os.path.abspath(path.dirname(__file__))
with open(os.path.join(path, 'README.md'), encoding='utf-8') as fd:
    long_description = fd.read()


setup(
    name='pynntp',
    version='1.0.2',
    description='NNTP Library (including compressed headers)',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Byron Platt',
    author_email='byron.platt@gmail.com',
    license='GPL3',
    url='https://github.com/greenbender/pynntp',
    packages=['nntp'],
    install_requires=['dateutils'],
    python_requires='>=2.7',
)
