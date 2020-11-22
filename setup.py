from setuptools import setup


setup(
    name='pynntp',
    version='0.9.0',
    description='NNTP Library (including compressed headers)',
    author='Byron Platt',
    author_email='byron.platt@gmail.com',
    license='GPL3',
    url='https://github.com/greenbender/pynntp',
    packages=['nntp'],
    install_requires=['dateutils'],
    python_requires='>=2.7',
)
