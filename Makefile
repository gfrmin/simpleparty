.PHONY: build publish clean

build: clean
	uv build

publish: build
	uv publish --token "$$(awk '/password/{print $$3}' ~/.pypirc)"

clean:
	rm -rf dist/
