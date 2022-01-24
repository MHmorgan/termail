from setuptools import setup

name='termail'

setup(
   name=name,
   version='1.0.0',
   description='Terminal mail client.',
   author='Magnus Aa. Hirth',
   author_email='magnus.hirth@gmail.com',
   packages=[name],
   install_requires=[
       'wheel',
       'click',
       'requests'
    ],
   scripts=['scripts/termail'],
)
