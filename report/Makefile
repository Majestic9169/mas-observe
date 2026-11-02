SRC := ./report.md
DST := ./report.pdf

view: pdf
	sioyek report.pdf

pdf: report.md
	pandoc ${SRC} -o ${DST} --pdf-engine=typst --toc

clean: 
	rm report.pdf

.PHONY: view clean
